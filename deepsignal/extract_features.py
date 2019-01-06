"""the feature extraction module of deepsignal.
output format:
chrom, pos, alignstrand, pos_in_strand, readname, read_strand, k_mer, signal_means,
signal_stds, signal_lens, cent_signals, methy_label
"""

import sys
import argparse
import time
import h5py
import random
import numpy as np
import multiprocessing as mp
from statsmodels import robust

from utils.process_utils import iupac_alphabets

from utils.process_utils import str2bool
from utils.process_utils import get_fast5s
from utils.process_utils import get_refloc_of_methysite_in_motif
from utils.ref_reader import get_contig2len

reads_group = 'Raw/Reads'
MAX_LEGAL_SIGNAL_NUM = 800  # 800 only for 17-mer


def _get_label_raw(fast5_fn, correct_group, correct_subgroup):
    try:
        fast5_data = h5py.File(fast5_fn, 'r')
    except IOError:
        raise IOError('Error opening file. Likely a corrupted file.')

    # Get raw data
    try:
        raw_dat = list(fast5_data[reads_group].values())[0]
        # raw_attrs = raw_dat.attrs
        raw_dat = raw_dat['Signal'].value
    except Exception:
        raise RuntimeError('Raw data is not stored in Raw/Reads/Read_[read#] so '
                           'new segments cannot be identified.')

    # Get Events
    try:
        event = fast5_data['/Analyses/'+correct_group + '/' + correct_subgroup + '/Events']
        corr_attrs = dict(list(event.attrs.items()))
    except Exception:
        raise RuntimeError('events not found.')

    read_start_rel_to_raw = corr_attrs['read_start_rel_to_raw']
    # print(event)
    # print('read_start_rel_to_raw: ',read_start_rel_to_raw)
    starts = list(map(lambda x: x+read_start_rel_to_raw, event['start']))
    lengths = event['length'].astype(np.int)
    base = [x.decode("UTF-8") for x in event['base']]
    assert len(starts) == len(lengths)
    assert len(lengths) == len(base)
    events = list(zip(starts, lengths, base))
    return raw_dat, events


def _get_alignment_attrs_of_each_strand(strand_path, h5obj):
    strand_basecall_group_alignment = h5obj['/'.join([strand_path, 'Alignment'])]
    alignment_attrs = strand_basecall_group_alignment.attrs
    # attr_names = list(alignment_attrs.keys())

    if strand_path.endswith('template'):
        strand = 't'
    else:
        strand = 'c'
    if sys.version_info[0] >= 3:
        try:
            alignstrand = str(alignment_attrs['mapped_strand'], 'utf-8')
            chrom = str(alignment_attrs['mapped_chrom'], 'utf-8')
        except TypeError:
            alignstrand = str(alignment_attrs['mapped_strand'])
            chrom = str(alignment_attrs['mapped_chrom'])
    else:
        alignstrand = str(alignment_attrs['mapped_strand'])
        chrom = str(alignment_attrs['mapped_chrom'])
    chrom_start = alignment_attrs['mapped_start']

    return strand, alignstrand, chrom, chrom_start


def _get_readid_from_fast5(h5file):
    first_read = list(h5file[reads_group].keys())[0]
    if sys.version_info[0] >= 3:
        try:
            read_id = str(h5file['/'.join([reads_group, first_read])].attrs['read_id'], 'utf-8')
        except TypeError:
            read_id = str(h5file['/'.join([reads_group, first_read])].attrs['read_id'])
    else:
        read_id = str(h5file['/'.join([reads_group, first_read])].attrs['read_id'])
    # print(read_id)
    return read_id


def _get_alignment_info_from_fast5(fast5_path, corrected_group='RawGenomeCorrected_000',
                                   basecall_subgroup='BaseCalled_template'):
    try:
        h5file = h5py.File(fast5_path, mode='r')
        corrgroup_path = '/'.join(['Analyses', corrected_group])

        if '/'.join([corrgroup_path, basecall_subgroup, 'Alignment']) in h5file:
            # fileprefix = os.path.basename(fast5_path).split('.fast5')[0]
            readname = _get_readid_from_fast5(h5file)
            strand, alignstrand, chrom, chrom_start = _get_alignment_attrs_of_each_strand('/'.join([corrgroup_path,
                                                                                                    basecall_subgroup]),
                                                                                          h5file)

            h5file.close()
            return readname, strand, alignstrand, chrom, chrom_start
        else:
            return '', '', '', '', ''
    except IOError:
        print("the {} can't be opened".format(fast5_path))
        return '', '', '', '', ''


def _normalize_signals(signals, normalize_method="mad"):
    if normalize_method == 'zscore':
        sshift, sscale = np.mean(signals), np.float(np.std(signals))
    elif normalize_method == 'mad':
        sshift, sscale = np.median(signals), np.float(robust.mad(signals))
    else:
        raise ValueError("")
    norm_signals = (signals - sshift) / sscale
    return np.around(norm_signals, decimals=6)


def _convert_motif_seq(ori_seq):
    outbases = []
    for bbase in ori_seq:
        outbases.append(iupac_alphabets[bbase])

    def recursive_permute(bases_list):
        if len(bases_list) == 1:
            return bases_list[0]
        elif len(bases_list) == 2:
            pseqs = []
            for fbase in bases_list[0]:
                for sbase in bases_list[1]:
                    pseqs.append(fbase + sbase)
            return pseqs
        else:
            pseqs = recursive_permute(bases_list[1:])
            pseq_list = [bases_list[0], pseqs]
            return recursive_permute(pseq_list)
    return recursive_permute(outbases)


def _get_motif_seqs(motifs):
    ori_motif_seqs = motifs.split(',')

    motif_seqs = []
    for ori_motif in ori_motif_seqs:
        motif_seqs += _convert_motif_seq(ori_motif)
    return motif_seqs


def _get_central_signals(signals_list, rawsignal_num=360):
    signal_lens = [len(x) for x in signals_list]

    if sum(signal_lens) < rawsignal_num:
        # real_signals = sum(signals_list, [])
        real_signals = np.concatenate(signals_list)
        cent_signals = np.append(real_signals, np.array([0] * (rawsignal_num - len(real_signals))))
    else:
        mid_loc = int((len(signals_list) - 1) / 2)
        mid_base_len = len(signals_list[mid_loc])

        if mid_base_len >= rawsignal_num:
            allcentsignals = signals_list[mid_loc]
            cent_signals = [allcentsignals[x] for x in sorted(random.sample(range(len(allcentsignals)),
                                                                            rawsignal_num))]
        else:
            left_len = (rawsignal_num - mid_base_len) // 2
            right_len = rawsignal_num - left_len

            # left_signals = sum(signals_list[:mid_loc], [])
            # right_signals = sum(signals_list[mid_loc:], [])
            left_signals = np.concatenate(signals_list[:mid_loc])
            right_signals = np.concatenate(signals_list[mid_loc:])

            if left_len > len(left_signals):
                right_len = right_len + left_len - len(left_signals)
                left_len = len(left_signals)
            elif right_len > len(right_signals):
                left_len = left_len + right_len - len(right_signals)
                right_len = len(right_signals)

            assert (right_len + left_len == rawsignal_num)
            if left_len == 0:
                cent_signals = right_signals[:right_len]
            else:
                cent_signals = np.append(left_signals[-left_len:], right_signals[:right_len])
    return cent_signals


def _extract_features(fast5s, corrected_group, basecall_subgroup, normalize_method,
                      motif_seqs, methyloc, chrom2len, kmer_len, raw_signals_len,
                      methy_label):
    features_str = []
    error = 0
    for fast5_fp in fast5s:
        try:
            raw_signal, events = _get_label_raw(fast5_fp, corrected_group, basecall_subgroup)
            norm_signals = _normalize_signals(raw_signal, normalize_method)
            genomeseq, signal_list = "", []
            for e in events:
                genomeseq += str(e[2])
                signal_list.append(norm_signals[e[0]:(e[0] + e[1])])

            readname, strand, alignstrand, chrom, \
                chrom_start = _get_alignment_info_from_fast5(fast5_fp, corrected_group, basecall_subgroup)

            chromlen = chrom2len[chrom]
            if alignstrand == '+':
                chrom_start_in_alignstrand = chrom_start
            else:
                chrom_start_in_alignstrand = chromlen - (chrom_start + len(genomeseq))

            cpg_site_locs = []
            for mseq in motif_seqs:
                cpg_site_locs += get_refloc_of_methysite_in_motif(genomeseq, mseq, methyloc)

            if kmer_len % 2 == 0:
                raise ValueError("kmer_len must be odd")
            num_bases = (kmer_len - 1) // 2

            for cpgloc_in_read in cpg_site_locs:
                if num_bases <= cpgloc_in_read < len(genomeseq) - num_bases:
                    cpgloc_in_ref = cpgloc_in_read + chrom_start_in_alignstrand

                    # cpgid = readname + chrom + alignstrand + str(cpgloc_in_ref) + strand
                    if alignstrand == '-':
                        pos = chromlen - 1 - cpgloc_in_ref
                    else:
                        pos = cpgloc_in_ref

                    k_mer = genomeseq[(cpgloc_in_read - num_bases):(cpgloc_in_read + num_bases + 1)]
                    k_signals = signal_list[(cpgloc_in_read - num_bases):(cpgloc_in_read + num_bases + 1)]

                    signal_lens = [len(x) for x in k_signals]
                    # if sum(signal_lens) > MAX_LEGAL_SIGNAL_NUM:
                    #     continue

                    signal_means = [np.mean(x) for x in k_signals]
                    signal_stds = [np.std(x) for x in k_signals]

                    cent_signals = _get_central_signals(k_signals, raw_signals_len)

                    means_text = ','.join([str(x) for x in np.around(signal_means, decimals=6)])
                    stds_text = ','.join([str(x) for x in np.around(signal_stds, decimals=6)])
                    signal_len_text = ','.join([str(x) for x in signal_lens])
                    cent_signals_text = ','.join([str(x) for x in cent_signals])

                    features_str.append("\t".join([chrom, str(pos), alignstrand, str(cpgloc_in_ref),
                                                   readname, strand, k_mer, means_text,
                                                   stds_text, signal_len_text, cent_signals_text,
                                                   str(methy_label)]))

        except Exception:
            error += 1
            continue
    print("extracted success {} of {}".format(len(fast5s) - error, len(fast5s)))
    # print("features_str len {}".format(len(features_str)))
    return features_str, error


def _fill_files_queue(fast5s_q, fast5_files, batch_size):
    for i in np.arange(0, len(fast5_files), batch_size):
        fast5s_q.put(fast5_files[i:(i+batch_size)])
    return


def _get_a_batch_features_str(fast5s_q, featurestr_q,
                              corrected_group, basecall_subgroup, normalize_method,
                              motif_seqs, methyloc, chrom2len, kmer_len, raw_signals_len, methy_label):
    while not fast5s_q.empty():
        try:
            fast5s = fast5s_q.get()
        except Exception:
            break
        features_str, error_num = _extract_features(fast5s, corrected_group, basecall_subgroup,
                                                    normalize_method, motif_seqs, methyloc,
                                                    chrom2len, kmer_len, raw_signals_len, methy_label)
        featurestr_q.put(features_str)


def _write_featurestr_to_file(write_fp, featurestr_q):
    with open(write_fp, 'w') as wf:
        while True:
            # during test, it's ok without the sleep(10)
            if featurestr_q.empty():
                time.sleep(10)
            features_str = featurestr_q.get()
            if features_str == "kill":
                break
            for one_features_str in features_str:
                wf.write(one_features_str + "\n")
            wf.flush()


def extract_features(fast5_files, batch_size, write_fp, nproc,
                     corrected_group, basecall_subgroup, normalize_method,
                     motif_seqs, methyloc, chrom2len, kmer_len, raw_signals_len, methy_label):
    start = time.time()

    fast5s_q = mp.Queue()
    _fill_files_queue(fast5s_q, fast5_files, batch_size)

    featurestr_q = mp.Queue()

    featurestr_procs = []
    if nproc > 1:
        nproc -= 1
    for _ in range(nproc):
        p = mp.Process(target=_get_a_batch_features_str, args=(fast5s_q, featurestr_q, corrected_group,
                                                               basecall_subgroup, normalize_method, motif_seqs,
                                                               methyloc, chrom2len, kmer_len, raw_signals_len,
                                                               methy_label))
        p.daemon = True
        p.start()
        featurestr_procs.append(p)

    print("write_process started..")
    p_w = mp.Process(target=_write_featurestr_to_file, args=(write_fp, featurestr_q))
    p_w.daemon = True
    p_w.start()

    for p in featurestr_procs:
        p.join()

    featurestr_q.put("kill")
    time.sleep(1)

    p_w.join()

    print("extract_features cost %.1f seconds.." % (time.time() - start))


def main():
    extraction_parser = argparse.ArgumentParser("extract features from corrected (tombo) fast5s for "
                                                "training or testing using deepsignal. "
                                                "\nIt is suggested that running this module 1 flowcell a time, "
                                                "or a group of flowcells a time, "
                                                "if the whole data is extremely large.")
    extraction_parser.add_argument("--fast5_dir", "-i", action="store", type=str,
                                   required=True,
                                   help="the directory of fast5 files")
    extraction_parser.add_argument("--corrected_group", action="store", type=str, required=False,
                                   default='RawGenomeCorrected_000',
                                   help='the corrected_group of fast5 files after '
                                        'tombo re-squiggle. default RawGenomeCorrected_000')
    extraction_parser.add_argument("--basecall_subgroup", action="store", type=str, required=False,
                                   default='BaseCalled_template',
                                   help='the corrected subgroup of fast5 files. default BaseCalled_template')
    extraction_parser.add_argument("--recursively", "-r", action="store", type=str, required=False,
                                   default='yes',
                                   help='is to find fast5 files from fast5_dir recursively. '
                                        'default true, t, yes, 1')
    extraction_parser.add_argument("--normalize_method", action="store", type=str, choices=["mad", "zscore"],
                                   default="mad", required=False,
                                   help="the way for normalizing signals in read level. "
                                        "mad or zscore, default mad")

    extraction_parser.add_argument("--reference_path", action="store",
                                   type=str, required=True,
                                   help="the genome reference file to be used, normally is a .fa file")
    extraction_parser.add_argument("--write_path", '-o', action="store",
                                   type=str, required=True,
                                   help='file path to save the features')
    extraction_parser.add_argument("--methy_label", action="store", type=str,
                                   choices=["1", '0'], required=False, default="1",
                                   help="the label of the interested modified bases, 0 or 1, "
                                        "default 1")
    extraction_parser.add_argument("--kmer_len", action="store",
                                   type=int, required=False, default=17,
                                   help="len of kmer. default 17")
    extraction_parser.add_argument("--cent_signals_len", action="store",
                                   type=int, required=False, default=360,
                                   help="the number of signals to be used in deepsignal, default 360")
    extraction_parser.add_argument("--motifs", action="store", type=str,
                                   required=False, default='CG',
                                   help='motif seq, default: CG. can be multi motifs splited by comma, '
                                        'or use IUPAC alphabet, '
                                        'but the mod_loc must be '
                                        'the same')
    extraction_parser.add_argument("--mod_loc", action="store", type=int, required=False, default=0,
                                   help='0-based location of the targeted base in the motif, default 0')
    # extraction_parser.add_argument("--region", action="store", type=str,
    #                                required=False, default=None,
    #                                help="region of interest, e.g.: chr1:0-10000, default None, "
    #                                     "for the whole region")

    extraction_parser.add_argument("--nproc", "-p", action="store", type=int, default=1,
                                   required=False,
                                   help="number of processes to be used")
    extraction_parser.add_argument("--batch_num", "-b", action="store", type=int, default=100,
                                   required=False,
                                   help="number of files to be processed by one process")

    extraction_args = extraction_parser.parse_args()

    fast5_dir = extraction_args.fast5_dir
    is_recursive = str2bool(extraction_args.recursively)

    corrected_group = extraction_args.corrected_group
    basecall_subgroup = extraction_args.basecall_subgroup
    normalize_method = extraction_args.normalize_method

    reference_path = extraction_args.reference_path
    write_path = extraction_args.write_path

    kmer_len = extraction_args.kmer_len
    cent_signals_num = extraction_args.cent_signals_len
    motifs = extraction_args.motifs
    mod_loc = extraction_args.mod_loc
    methy_label = extraction_args.methy_label

    nproc = extraction_args.nproc
    batch_num = extraction_args.batch_num

    fast5_files = get_fast5s(fast5_dir, is_recursive)
    print("{} fast5 files in total".format(len(fast5_files)))

    print("reading genome reference file..")
    chrom2len = get_contig2len(reference_path)
    motif_seqs = _get_motif_seqs(motifs)

    extract_features(fast5_files, batch_num, write_path, nproc,
                     corrected_group, basecall_subgroup, normalize_method,
                     motif_seqs, mod_loc, chrom2len, kmer_len, cent_signals_num, methy_label)


if __name__ == '__main__':
    sys.exit(main())
