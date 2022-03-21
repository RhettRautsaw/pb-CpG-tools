#!/usr/bin/env python
# coding: utf-8
import argparse
import logging
import numpy as np
import pandas as pd
import pyBigWig
import pysam
import os
import re
from array import array
from Bio import SeqIO
from Bio.Seq import Seq
from multiprocessing import Pool
from numpy.lib.stride_tricks import sliding_window_view
from operator import itemgetter
from tqdm import tqdm, trange
from tqdm.contrib.concurrent import process_map
from scipy.stats import spearmanr
os.environ["CUDA_VISIBLE_DEVICES"] = ""

def get_args():
    """
    Get arguments from command line with argparse.
    """
    parser = argparse.ArgumentParser(
        prog='RefAlnBam-to-ModsBed.py',
        description="""Calculate CpG positions and scores from an aligned bam file. Outputs raw and 
        coverage-filtered results in bed and bigwig format, including haplotype-specific results (when available).""")

    parser.add_argument("-b", "--bam",
                        required=True,
                        metavar="input.bam",
                        help="The aligned BAM file.")
    parser.add_argument("-f", "--fasta",
                        required=True,
                        metavar="ref.fasta",
                        help="The reference fasta file.")
    parser.add_argument("-o", "--output_label",
                        required=True,
                        metavar="label",
                        help="Label for output files, which results in [label].bed/bw.")
    parser.add_argument("-p", "--pileup_mode",
                        required=False,
                        choices=["model", "count"],
                        default="count",
                        help="Use a model-based approach to score modifications across sites (model) "
                             "or a simple count-based approach (count). [default = %(default)s]")
    parser.add_argument("-d", "--model_dir",
                        required=False,
                        default=None,
                        metavar="/path/to/model/dir",
                        help="Full path to the directory containing the model (*.pb files) to load. [default = None]")
    parser.add_argument("-m", "--modsites",
                        required=False,
                        choices=["denovo", "reference"],
                        default="denovo",
                        help="Only output CG sites with a modification probability > 0 "
                             "(denovo), or output all CG sites based on the "
                             "supplied reference fasta (reference). [default = %(default)s]")
    parser.add_argument("-c", "--min_coverage",
                        required=False,
                        default=4,
                        type=int,
                        metavar="int",
                        help="Minimum coverage required for filtered outputs. [default: %(default)d]")
    parser.add_argument("-q", "--min_mapq",
                        required=False,
                        default=0,
                        type=int,
                        metavar="int",
                        help="Ignore alignments with MAPQ < N. [default: %(default)d]")
    parser.add_argument("-a", "--hap_tag",
                        required=False,
                        default="HP",
                        metavar="TAG",
                        help="The SAM tag containing haplotype information. [default: %(default)s]")
    parser.add_argument("-s", "--chunksize",
                        required=False,
                        default=500000,
                        type=int,
                        metavar="int",
                        help="Break reference regions into chunks "
                             "of this size for parallel processing. [default = %(default)d]")
    parser.add_argument("-t", "--threads",
                        required=False,
                        default=1,
                        type=int,
                        metavar="int",
                        help="Number of threads for parallel processing. [default = %(default)d]")

    return parser.parse_args()

def setup_logging(output_label):
    """
    Set up logging to file.
    """
    logname = "{}-aligned_bam_to_cpg_scores.log".format(output_label)
    # ensure logging file does not exist, if so remove
    if os.path.exists(logname):
        os.remove(logname)

    # set up logging to file
    logging.basicConfig(filename=logname,
                        format="%(asctime)s: %(levelname)s: %(message)s",
                        datefmt='%d-%b-%y %H:%M:%S',
                        level=logging.DEBUG)

def get_regions_to_process(input_bam, input_fasta, chunksize, modsites, pileup_mode, model_dir, min_mapq, hap_tag):
    """
    Breaks reference regions into smaller regions based on chunk
    size specified. Returns a list of lists that can be used for
    multiprocessing. Each sublist contains:
    [bam path (str), fasta path (str), modsites (str),
    reference name (str), start coordinate (int), stop coordinate (int)]

    :param input_bam: Path to input bam file. (str)
    :param input_fasta: Path to reference fasta file. (str)
    :param chunksize: Chunk size (default = 500000). (int)
    :param modsites: Filtering method. (str: "denovo", "reference")
    :param pileup_mode: Site modification calling method. (str: "model", "count")
    :param model_dir: Full path to model directory to load (if supplied), otherwise is None.
    :param min_mapq: Minimum mapping quality score. (int)
    :param hap_tag: The SAM tag label containing haplotype information. (str)
    :return regions_to_process: List of lists containing region sizes. (list)
    """
    logging.info("get_regions_to_process: Starting chunking.")
    # open the input bam file with pysam
    bamIn = pysam.AlignmentFile(input_bam, 'rb')
    # empty list to store sublists with region information
    regions_to_process = []
    # iterate over reference names and their corresponding lengths
    references = zip(bamIn.references, bamIn.lengths)
    for ref, length in references:
        start = 1
        while start < length:
            end = start + chunksize
            if end < length:
                regions_to_process.append([input_bam, input_fasta, modsites, pileup_mode, model_dir, ref, start, end - 1, min_mapq, hap_tag])
            else:
                regions_to_process.append([input_bam, input_fasta, modsites, pileup_mode, model_dir, ref, start, length, min_mapq, hap_tag])
            start = start + chunksize
    # close bam
    bamIn.close()
    logging.info("get_regions_to_process: Created {:,} region chunks.\n".format(len(regions_to_process)))

    return regions_to_process

def cg_sites_from_fasta(input_fasta, ref):
    """
    Gets all CG site positions from a given reference region, and
    make positions keys in a dict with empty strings as vals.

    :param input_fasta: A path to reference fasta file. (str)
    :param ref: Reference name. (str)
    :return cpg_sites_dict: Dictionary with all CG ref positions as keys, empty string vals. (dict)
    """
    # open fasta with BioPython and iterated over records
    with open(input_fasta) as fh:
        for record in SeqIO.parse(fh, "fasta"):
            # if record name matches this particular ref,
            if record.id == ref:
                # use regex to find all indices for 'CG' in the reference seq, e.g. the C positions
                cg_sites_dict = {i.start():"" for i in re.finditer('CG', str(record.seq.upper()))}
                # there may be some stretches without any CpGs in a reference region
                # handle these edge cases by adding a dummy value of -1 (an impossible coordinate)
                if not any(cg_sites_dict.keys()):
                    cg_sites_dict[-1] = ""
                # once seq is found, stop iterating
                break
    # make sure the ref region was matched to a ref fasta seq
    if not any(cg_sites_dict.keys()):
        logging.error("cg_sites_from_fasta: The sequence '{}' was not found in the reference fasta file.".format(ref))
        raise ValueError('The sequence "{}" was not found in the reference fasta file!'.format(ref))

    return cg_sites_dict

def get_mod_sequence(integers):
    """
    Convert a list of integers coding mod bases from the SAM Mm tags into
    a list of positions of sequential bases.
    Example: [5, 12, 0] -> [6, 19, 20]
    In above example the 6th C, 19th C, and 20th C are modified
    See this example described in: https://samtools.github.io/hts-specs/SAMtags.pdf; Dec 9 2021

    :param integers: List of integers (parsed from SAM Mm tag). (list)
    :return mod_sequence: List of integers, 1-based counts of position of modified base in set of bases. (list)
    """
    mod_sequence = []
    base_count = int(0)
    for i in integers:
        base_count += i + 1
        mod_sequence.append(base_count)
    return mod_sequence

def get_base_indices(query_seq, base, reverse):
    """
    Find all occurrences of base in query sequence and make a list of their
    indices. Return the list of indices.

    :param query_seq: The original read sequence (not aligned read sequence). (str)
    :param base: The nucleotide modifications occur on ('C'). (str)
    :param reverse: True/False whether sequence is reversed. (Boolean)
    :return: List of integers, 0-based indices of all bases in query seq. (list)
    """
    if reverse == False:
        return [i.start() for i in re.finditer(base, query_seq)]
    # if seq stored in reverse, need reverse complement to get correct indices for base
    # use biopython for this (convert to Seq, get RC, convert to string)
    else:
        return [i.start() for i in re.finditer(base, str(Seq(query_seq).reverse_complement()))]

def parse_mmtag(query_seq, mmtag, modcode, base, reverse):
    """
    Get a list of the 0-based indices of the modified bases in
    the query sequence.

    :param query_seq: The original read sequence (not aligned read sequence). (str)
    :param mmtag: The Mm tag obtained for the read ('C+m,5,12,0;'). (str)
    :param modcode: The modification code to search for in the tag ('C+m'). (str)
    :param base: The nucleotide modifications occur on ('C'). (str)
    :param reverse: True/False whether sequence is reversed. (Boolean)
    :return mod_base_indices: List of integers, 0-based indices of all mod bases in query seq. (list)
    """
    # tags are written as: C+m,5,12,0;C+h,5,12,0;
    # if multiple mod types present in tag, must find relevant one first
    modline = [x.replace(modcode + ',', '') for x in mmtag.split(';') if x.startswith(modcode)]
    # gives a list with one sublist containing a string if mod type found: [["5,12,0"]]
    if modline:
        # first get the sequence of the mod bases from tag integers
        # this is a 1-based position of each mod base in the complete set of this base from this read
        # e.g., [6, 19, 20] = the 6th, 19th, and 20th C bases are modified in the set of Cs
        mod_sequence = get_mod_sequence([int(x) for x in modline[0].split(',')])
        # get all 0-based indices of this base in this read, e.g. every C position
        base_indices = get_base_indices(query_seq, base, reverse)
        # use the mod sequence to identify indices of the mod bases in the read
        mod_base_indices = [base_indices[i - 1] for i in mod_sequence]
    else:
        mod_base_indices = []

    return mod_base_indices

def parse_mltag(mltag):
    """
    Convert 255 discrete integer code into mod score 0-1, return as list.

    This is NOT designed to handle interleaved Ml format for multiple mod types!

    :param mltag: The Ml tag obtained for the read with('Ml:B:C,204,89,26'). (str)
    :return: List of floats, probabilities of all mod bases in query seq. (list)
    """
    return [round(x / 256, 3) if x > 0 else 0 for x in mltag]

def get_mod_dict(query_seq, mmtag, modcode, base, mltag, reverse):
    """
    Make a dictionary from the Mm and Ml tags, in which the
    modified base index (in the query seq) is the key and the
    mod score is the value.

    This is NOT designed to handle interleaved Ml format for multiple mod types!

    :param query_seq: The original read sequence (not aligned read sequence). (str)
    :param mmtag: The Mm tag obtained for the read ('C+m,5,12,0;'). (str)
    :param modcode: The modification code to search for in the tag ('C+m'). (str)
    :param base: The nucleotide modifications occur on ('C'). (str)
    :param mltag: The Ml tag obtained for the read with('Ml:B:C,204,89,26'). (str)
    :param reverse: True/False whether sequence is reversed. (Boolean)
    :return mod_dict: Dictionary with mod positions and scores. (dict)
    """
    mod_base_indices = parse_mmtag(query_seq, mmtag, modcode, base, reverse)
    mod_scores = parse_mltag(mltag)
    mod_dict = dict(zip(mod_base_indices, mod_scores))
    return mod_dict

def pileup_from_reads(bamIn, ref, pos_start, pos_stop, min_mapq, hap_tag):
    """
    For a given region, retrieve all reads.
    For each read, iterate over positions aligned to this region.
    Build a dictionary with ref positions as keys. Each key will have a
    value that is a list of sublists. Each sublist comes from a read
    aligned to that site. The sublist will contain the strand information,
    modification score, and haplotype:
    [strand symbol (str), mod score (float), haplotype (int)]

    Return the unfiltered  dictionary.

    :param bamIn: AlignmentFile object of input bam file.
    :param ref: Reference name. (str)
    :param pos_start: Start coordinate for region. (int)
    :param pos_stop: Stop coordinate for region. (int)
    :param min_mapq: Minimum mapping quality score. (int)
    :param hap_tag: Name of SAM tag containing haplotype information. (str)
    :return data_dict: Unfiltered dictionary with reference positions as keys, vals = list of lists. (dict)
    """
    logging.debug("coordinates {}: {:,}-{:,}: (2) pileup_from_reads".format(ref, pos_start, pos_stop))
    data_dict = {}
    # iterate over all reads present in this region
    for read in bamIn.fetch(contig=ref, start=pos_start, stop=pos_stop):
        # check if passes minimum mapping quality score
        if read.mapping_quality < min_mapq:
            #logging.warning("pileup_from_reads: read did not pass minimum mapQV: {}".format(read.query_name))
            continue
        # identify the haplotype tag, if any (default tag = HP)
        # values are 1 or 2 (for haplotypes), or 0 (no haplotype)
        try:
            hap = read.get_tag(hap_tag)
        except KeyError:
            hap = int(0)
        # check for SAM-spec methylation tags
        # draft tags were Ml and Mm, accepted tags are now ML and MM
        # check for both types, set defaults to None and change if found
        mmtag, mltag = None, None
        try:
            mmtag = read.get_tag('Mm')
            mltag = read.get_tag('Ml')
        except KeyError:
            pass
        try:
            mmtag = read.get_tag('MM')
            mltag = read.get_tag('ML')
        except KeyError:
            pass

        if mmtag is not None and mltag is not None:
            # note that this could potentially be used for other mod types, but
            # the Mm and Ml parsing functions are not set up for the interleaved format
            # e.g.,  ‘Mm:Z:C+mh,5,12; Ml:B:C,204,26,89,130’ does NOT work
            # to work it must be one mod type, and one score per mod position
            mod_dict = get_mod_dict(read.query_sequence, mmtag, 'C+m', 'C', mltag, read.is_reverse)

            if read.get_aligned_pairs(matches_only=True)[20:-20]:
                # iterate over positions
                for query_pos, ref_pos in read.get_aligned_pairs(matches_only=True)[20:-20]:
                    # make sure ref position is in range of ref target region
                    if ref_pos >= pos_start and ref_pos <= pos_stop:
                        # identify if read is reverse strand or forward to set correct values
                        if read.is_reverse:
                            strand, location = "-", (len(read.query_sequence) - query_pos - 2)
                        else:
                            strand, location = "+", query_pos
                        # check if this position has a mod score in the dictionary,
                        # if not assign score of zero
                        if location not in mod_dict:
                            score = 0
                        else:
                            score = mod_dict[location]
                        # check if this reference position is a key in the dictionary yet
                        # add sublist with strand, modification score, and haplotype to the value list
                        try:
                            data_dict[ref_pos].append([strand, score, hap])
                        except KeyError:
                            data_dict[ref_pos] = [[strand, score, hap]]
        # if no SAM-spec methylation tags present, ignore read and log
        else:
            logging.warning("pileup_from_reads: read missing MM and/or ML tag(s): {}".format(read.query_name))

    return data_dict

def filter_data_dict(data_dict, ref, pos_start, pos_stop, input_fasta, modsites):
    """
    Filter the mod sites dictionary based on the modsites option selected:
    "reference": Keep all sites that match a reference CG site (this includes both
                 modified and unmodified sites). It will exclude all modified sites
                 that are not CG sites, according to the ref sequence.
    "denovo": Keep all sites which have at least one modification score > 0, per strand.
              This can include sites that are CG in the reads, but not in the reference.
              It can exclude CG sites with no modifications on either strand from being
              written to the bed file.

    Return the filtered dictionary.

    :param data_dict: Dictionary object from pileup_from_reads(). (dict)
    :param ref: A path to reference fasta file. (str)
    :param pos_start: Start coordinate for region. (int)
    :param pos_stop: Stop coordinate for region. (int)
    :param modsites: Filtering method. (str: "denovo", "reference")
    :param ref: Reference name. (str)
    :return filtered_dict: Dictionary with reference positions as keys, vals = list of lists. (dict)
    """
    if modsites == "reference":
        # if there are alignments for this region, get all CG sites from the reference
        if data_dict:
            # get CG ref site positions from reference
            cg_sites_dict = cg_sites_from_fasta(input_fasta, ref)
        # if no alignments, skip the cpg sites step and give a dummy coord
        else:
            cg_sites_dict = {-1:""}
        # keep all sites that match position of a reference CG site by
        # doing a fast lookup of each potential site in the cpg dict
        filtered_dict = {k:v for (k,v) in data_dict.items() if k in cg_sites_dict}
        logging.debug("coordinates {}: {:,}-{:,}: (3) filter_data_dict".format(ref, pos_start, pos_stop))

    elif modsites == "denovo":
        # filter dictionary to remove strand-specific sites for which there are no modified bases present
        # the values for each dict key are [[strand, modscore, hap], [strand, modscore, hap], ...]
        # this method is NOT haplotype aware, it searches by strands only!
        filtered_dict = {}
        for k, v in data_dict.items():
            # must check each strand separately for modified bases
            # first check forward strand bases
            if [j for j in [i for i in v if i[0] == "+"] if j[1] > 0.5]:
                try:
                    filtered_dict[k].extend([i for i in v if i[0] == "+"])
                except:
                    filtered_dict[k] = [i for i in v if i[0] == "+"]
            # then check reverse strand bases
            if [j for j in [i for i in v if i[0] == "-"] if j[1] > 0.5]:
                try:
                    filtered_dict[k].extend([i for i in v if i[0] == "-"])
                except:
                    filtered_dict[k] = [i for i in v if i[0] == "-"]
        logging.debug("coordinates {}: {:,}-{:,}: (3) filter_data_dict".format(ref, pos_start, pos_stop))

    return filtered_dict

def calc_stats(df):
    """
    Gets summary stats from a given dataframe p.
    :param df: Pandas dataframe.
    :return: Summary statistics
    """
    total = df.shape[0]
    mod = df[df['prob'] > 0.5].shape[0]
    unMod = df[df['prob'] <= 0.5].shape[0]

    modScore = "." if mod == 0 else str(round(df[df['prob'] > 0.5]['prob'].mean(), 3))
    unModScore = "." if unMod == 0 else str(round(df[df['prob'] <= 0.5]['prob'].mean(), 3))
    percentMod = 0.0 if mod == 0 else round((mod / total) * 100, 1)

    return percentMod, mod, unMod, modScore, unModScore

def collect_bed_results_count(ref, pos_start, pos_stop, filtered_dict):
    """
    Iterates over reference positions and corresponding sublists (k,v in filtered_dict).
    For each position, makes a pandas dataframe from the sublists.
    The dataframe is filtered for strands and haplotypes, and summary statistics are
    calculated with calc_stats().
    For each position and strand/haploytpe combination, a sublist of summary information
    is appended to the bed_results list:
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage,
    (6) mod sites, (7) unmod sites, (8) mod score, (9) unmod score]
    This information is used to write the output bed file.

    :param ref: Reference name. (str)
    :param pos_start: Start coordinate for region. (int)
    :param pos_stop: Stop coordinate for region. (int)
    :param filtered_dict: Dictionary from pileup_from_reads(). (dict)
    :return bed_results: List of sublists with information to write the output bed file. (list)
    """
    logging.debug("coordinates {}: {:,}-{:,}: (4) collect_bed_results_count".format(ref, pos_start, pos_stop))
    # intiate empty list to store bed sublists
    bed_results = []

    # iterate over the ref positions and corresponding vals
    for refPosition, modinfoList in sorted(filtered_dict.items()):
        # create pandas dataframe from this list of sublists
        df = pd.DataFrame(modinfoList, columns=['strand', 'prob', 'hap'])

        # Filter dataframe based on strand/haplotype combinations, get information,
        # and create sublists and append to bed_results.
        # merged strands / haplotype 1
        percentMod, mod, unMod, modScore, unModScore = calc_stats(df[df['hap'] == 1])
        if mod + unMod >= 1:
            bed_results.append([ref, refPosition, (refPosition + 1), percentMod,
                                "hap1", mod + unMod, mod, unMod, modScore, unModScore])

        # merged strands / haplotype 2
        percentMod, mod, unMod, modScore, unModScore = calc_stats(df[df['hap'] == 2])
        if mod + unMod >= 1:
            bed_results.append([ref, refPosition, (refPosition + 1), percentMod,
                                "hap2", mod + unMod, mod, unMod, modScore, unModScore])

        # merged strands / both haplotypes
        percentMod, mod, unMod, modScore, unModScore = calc_stats(df)
        if mod + unMod >= 1:
            bed_results.append([ref, refPosition, (refPosition + 1), percentMod,
                                "Total", mod + unMod, mod, unMod, modScore, unModScore])

    return bed_results

def get_normalized_histo(df, adj):
    """
    Create the array data structure needed to apply the model, for a given site.

    :param df: Pandas dataframe object, created from filtered_dict values containing ['strand', 'prob', 'hap'].
    :param adj: A 0 or 1 indicating whether or not previous position was a CG. (int)
    :return: List with normalized histogram and coverage (if min coverage met), else returns None. (list)
    """
    cov = df.shape[0]
    if (cov >= 4):
        # create array containing 21 zeroes
        norm_hist = np.zeros(21, dtype=float)
        # create histogram from pileup probability scores, range 0-1 with bin sizes of 0.05
        # returns hist [0] and edges [1]
        hist = np.histogram(df['prob'], bins=20, range=[0, 1])[0]
        # get Euclidean norm for vector
        norm = np.linalg.norm(hist)
        # divide hist by norm and add values to array
        norm_hist[0:20] = hist / norm
        # add either 0 (not adjacent to a prior CG) or 1 (adjacent to a prior CG) to final spot in array
        norm_hist[20] = adj
        return [norm_hist, cov]
    else:
        return

def apply_model(refpositions, normhistos, coverages, ref, pos_start, pos_stop, model, hap):
    """
    Apply model to make modification calls for all sites using a sliding window approach.
    Create a list with results, ultimately for bed file:
        [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage]

    :param refpositions: List with all CG positions. (list)
    :param normhistos: List with all normalized histogram data structures. (list)
    :param coverages: List with all CG coverages. (list)
    :param ref: Reference contig name. (str)
    :param pos_start: Start coordinate for region. (int)
    :param pos_stop: Stop coordinate for region. (int)
    :param model: The tensorflow model object.
    :param hap: Label of haplotype (hap1, hap2, or Total). (str)
    :return temp_bed_results: List of information for bed files, but can possibly be blank. (list)
    """
    temp_bed_results = []

    if len(normhistos) > 11:

        featPad = np.pad(np.stack(normhistos), pad_width=((6, 4), (0, 0)), mode='constant', constant_values=0)
        featuresWindow = sliding_window_view(featPad, 11, axis=0)

        featuresWindow = np.swapaxes(featuresWindow, 1, 2)
        predict = model.predict(featuresWindow)

        predict = np.where(predict < 0, 0, predict)
        predict = np.where(predict > 1, 1, predict)

        for i, position in enumerate(refpositions):
            temp_bed_results.append([ref, position, (position + 1), round(predict[i][0] * 100, 1), hap, coverages[i]])

    else:
        logging.warning("coordinates {}: {:,}-{:,}: apply_model: insufficient data".format(ref, pos_start, pos_stop))

    return temp_bed_results

def collect_bed_results_model(ref, pos_start, pos_stop, filtered_dict, model_dir):
    """
    Iterates over reference positions and creates normalized histograms of scores,
    feeds all sites and scores into model function to assign modification probabilities,
    and creates a list of sublists for writing bed files:
         [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage]
    This information is returned and ultimately used to write the output bed file.

    :param ref: Reference name. (str)
    :param pos_start: Start coordinate for region. (int)
    :param pos_stop: Stop coordinate for region. (int)
    :param filtered_dict: Dictionary from pileup_from_reads(). (dict)
    :param model_dir: Full path to directory containing model. (str)
    :return bed_results: List of sublists with information to write the output bed file. (list)
    """
    logging.debug("coordinates {}: {:,}-{:,}: (4) collect_bed_results_model".format(ref, pos_start, pos_stop))

    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    import tensorflow as tf
    logging.getLogger('tensorflow').setLevel(logging.ERROR)

    # this may or may not do anything to help with the greedy thread situation...
    #tf.config.threading.set_intra_op_parallelism_threads(1)
    #tf.config.threading.set_inter_op_parallelism_threads(1)

    model = tf.keras.models.load_model(model_dir, compile=False)

    total_refpositions, total_normhistos, total_coverages = [], [], []
    hap1_refpositions, hap1_normhistos, hap1_coverages = [], [], []
    hap2_refpositions, hap2_normhistos, hap2_coverages = [], [], []

    # set initial C index for CG location to 0
    previousLocation = 0
    # iterate over keys (reference positions) and values (list containing [strand, score, hap]) in filtered_dict
    for refPosition, modinfoList in sorted(filtered_dict.items()):
        # determine if there is an adjacent prior CG, score appropriately
        if (refPosition - previousLocation) == 2:
            adj = 1
        else:
            adj = 0
        # update CG position
        previousLocation = refPosition

        # create pandas dataframe from this list of sublists
        df = pd.DataFrame(modinfoList, columns=['strand', 'prob', 'hap'])
        # build lists for combined haplotypes
        # returns [norm_hist, cov] if min coverage met, otherwise returns empty list
        total_result_list = get_normalized_histo(df, adj)
        if total_result_list:
            total_normhistos.append(total_result_list[0])
            total_coverages.append(total_result_list[1])
            total_refpositions.append(refPosition)

        # build lists for hap1
        hap1_result_list = get_normalized_histo(df[df['hap'] == 1], adj)
        if hap1_result_list:
            hap1_normhistos.append(hap1_result_list[0])
            hap1_coverages.append(hap1_result_list[1])
            hap1_refpositions.append(refPosition)

        # build lists for hap2
        hap2_result_list = get_normalized_histo(df[df['hap'] == 2], adj)
        if hap2_result_list:
            hap2_normhistos.append(hap2_result_list[0])
            hap2_coverages.append(hap2_result_list[1])
            hap2_refpositions.append(refPosition)

    # initiate empty list to store all bed results
    bed_results = []
    # run model for total, hap1, hap2, and add to bed results if non-empty list was returned
    total_temp_bed_results = (apply_model(total_refpositions, total_normhistos, total_coverages, ref, pos_start, pos_stop, model, "Total"))
    if total_temp_bed_results:
        bed_results.extend(total_temp_bed_results)

    hap1_temp_bed_results = (apply_model(hap1_refpositions, hap1_normhistos, hap1_coverages, ref, pos_start, pos_stop, model, "hap1"))
    if hap1_temp_bed_results:
        bed_results.extend(hap1_temp_bed_results)

    hap2_temp_bed_results = (apply_model(hap2_refpositions, hap2_normhistos, hap2_coverages, ref, pos_start, pos_stop, model, "hap2"))
    if hap2_temp_bed_results:
        bed_results.extend(hap2_temp_bed_results)

    return bed_results

def run_process_region(arguments):
    """
    Process a given reference region to identify modified bases.
    Uses pickled args (input_file, ref, pos_start, pos_stop) to run
    pileup_from_reads() to get all desired sites (based on modsites option),
    then runs collect_bed_results() to summarize information.

    The sublists will differ between model or count method, but they always share the first 7 elements:
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage, ...]

    :param arguments: Pickled list. (list)
    :return bed_results: List of sublists with information to write the output bed file. (list)
    """
    # unpack pickled items:
    # [bam path (str), fasta path (str), modsites option (str),
    #  pileup_mode option (str), model directory path (str),
    #  reference contig name (str), start coordinate (int),
    #  stop coordinate (int), minimum mapping QV (int), haplotype tag name (str)]
    input_bam, input_fasta, modsites, pileup_mode, model_dir, ref, pos_start, pos_stop, min_mapq, hap_tag = arguments
    logging.debug("coordinates {}: {:,}-{:,}: (1) run_process_region: start".format(ref, pos_start, pos_stop))
    # open the input bam file with pysam
    bamIn = pysam.AlignmentFile(input_bam, 'rb')
    # get all ref sites with mods and information from corresponding aligned reads
    data_dict = pileup_from_reads(bamIn, ref, pos_start, pos_stop, min_mapq, hap_tag)
    # filter based on denovo or reference sites
    filtered_dict =  filter_data_dict(data_dict, ref, pos_start, pos_stop, input_fasta, modsites)
    # bam object no longer needed, close file
    bamIn.close()

    # summarize the mod results, depends on pileup_mode option selected
    if pileup_mode == "count":
        bed_results = collect_bed_results_count(ref, pos_start, pos_stop, filtered_dict)
    elif pileup_mode == "model":
        bed_results = collect_bed_results_model(ref, pos_start, pos_stop, filtered_dict, model_dir)

    logging.debug("coordinates {}: {:,}-{:,}: (5) run_process_region: finish".format(ref, pos_start, pos_stop))

    if len(bed_results) > 1:
        return bed_results
    else:
        return

def run_all_pileup_processing(regions_to_process, threads):
    """
    Function to distribute jobs based on reference regions created.
    Collects results and returns list for writing output bed file.

    The bed results will differ based on model or count method, but they always share the first 7 elements:
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage, ...]

    :param regions_to_process: List of sublists defining regions (input_file, ref, pos_start, pos_stop). (list)
    :param threads: Number of threads to use for multiprocessing. (int)
    :return filtered_bed_results: List of sublists with information to write the output bed file. (list)
    """
    logging.info("run_all_pileup_processing: Starting parallel processing.\n")
    # set threads
    pool = Pool(processes=threads)
    # run all jobs
    bed_results = process_map(run_process_region, regions_to_process, max_workers=threads, miniters=threads, chunksize=1, smoothing=0)
    logging.info("run_all_pileup_processing: Finished parallel processing.\n")
    # results is a list of sublists, may contain None, remove these
    filtered_bed_results = [i for i in bed_results if i]
    # turn list of lists of sublists into list of sublists
    flattened_bed_results = [i for sublist in filtered_bed_results for i in sublist]

    # ensure bed results are sorted by ref contig name, start position
    logging.info("run_all_pileup_processing: Starting sort for bed results.\n")
    if flattened_bed_results:
        flattened_bed_results.sort(key=itemgetter(0, 1))
        logging.info("run_all_pileup_processing: Finished sort for bed results.\n")

    return flattened_bed_results

def write_output_bed(label, modsites, min_coverage, bed_results):
    """
    Writes output bed file(s) based on information in bed_merge_results (default).
    Separates results into total, hap1, and hap2. If haplotypes not available,
    only total is produced.

    The bed_merge_results list will contain slighty different information depending on the pileup_mode option,
    but the first 7 fields will be identical:

    count-based list
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage,
    (6) mod sites, (7) unmod sites, (8) mod score, (9) unmod score]

    OR
    model-based list
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage]

    :param outname: Name of output bed file to write. (str)
    :param modsites: "reference" or "denovo", for the CpG detection mode. (str)
    :param min_coverage: Minimum coverage to retain a site. (int)
    :param bed_results: List of sublists with information to write the output bed file. (list)
    :return output_files: List of output bed file names that were successfully written. (list)
    """
    logging.info("write_output_bed: Writing unfiltered output bed files.\n")
    out_total = "{}.combined.{}.bed".format(label, modsites)
    out_hap1 = "{}.hap1.{}.bed".format(label, modsites)
    out_hap2 = "{}.hap2.{}.bed".format(label, modsites)
    cov_total = "{}.combined.{}.mincov{}.bed".format(label, modsites, min_coverage)
    cov_hap1 = "{}.hap1.{}.mincov{}.bed".format(label, modsites, min_coverage)
    cov_hap2 = "{}.hap2.{}.mincov{}.bed".format(label, modsites, min_coverage)

    # remove any previous version of output files
    for f in [out_total, out_hap1, out_hap2, cov_total, cov_hap1, cov_hap2]:
        if os.path.exists(f):
            os.remove(f)

    with open(out_total, 'a') as fh_total:
        with open(out_hap1, 'a') as fh_hap1:
            with open(out_hap2, 'a') as fh_hap2:
                for i in bed_results:
                    if i[4] == "Total":
                        fh_total.write("{}\n".format("\t".join([str(j) for j in i])))
                    elif i[4] == "hap1":
                        fh_hap1.write("{}\n".format("\t".join([str(j) for j in i])))
                    elif i[4] == "hap2":
                        fh_hap2.write("{}\n".format("\t".join([str(j) for j in i])))

    # write coverage-filtered versions of bed files
    logging.info("write_output_bed: Writing coverage-filtered output bed files, using min coverage = {}.\n".format(min_coverage))
    output_files = []
    for inBed, covBed in [(out_total, cov_total), (out_hap1, cov_hap1), (out_hap2, cov_hap2)]:
        # if haplotypes not present, the bed files are empty, remove and do not write cov-filtered version
        if os.stat(inBed).st_size == 0:
            os.remove(inBed)
        else:
            output_files.append(inBed)
            # write coverage filtered bed file
            with open(inBed, 'r') as fh_in, open(covBed, 'a') as fh_out:
                for line in fh_in:
                    if int(line.split('\t')[5]) >= min_coverage:
                        fh_out.write(line)
            # check to ensure some sites were written, otherwise remove
            if os.stat(covBed).st_size == 0:
                os.remove(covBed)
            else:
                output_files.append(covBed)

    return output_files

def make_bed_df(bed, pileup_mode):
    """
    Construct a pandas dataframe from a bed file.

    count-based list
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage,
    (6) mod sites, (7) unmod sites, (8) mod score, (9) unmod score]

    OR
    model-based list
    [(0) ref name, (1) start coord, (2) stop coord, (3) % mod sites, (4) haplotype, (5) coverage]

    :param bed: Name of bed file.
    :param pileup_mode: Site modification calling method. (str: "model", "count")
    :return df: Pandas dataframe.
    """
    logging.debug("make_bed_df: Converting '{}' to pandas dataframe.\n".format(bed))
    if pileup_mode == "count":
        df = pd.read_csv(bed, sep='\t', header=None,
                         names = ['chromosome', 'start', 'stop', 'percent_modified', 'haplotype', 'coverage',
                                  'modified_bases', 'unmodified_bases', 'mod_score', 'unmod_score'])
        df.drop(columns=['modified_bases', 'unmodified_bases', 'mod_score', 'unmod_score', 'haplotype', 'coverage'], inplace=True)

    elif pileup_mode == "model":
        df = pd.read_csv(bed, sep='\t', header=None,
                         names = ['chromosome', 'start', 'stop', 'percent_modified', 'haplotype', 'coverage'])
        df.drop(columns=['haplotype', 'coverage'], inplace=True)

    #df.sort_values(by=['chromosome', 'start'], inplace=True)

    return df

def get_bigwig_header_info(input_fasta):
    """
    Get chromosome names and lengths from reference fasta.

    :param input_fasta: Name of reference fasta file.
    :return header: List of tuples, containing [ (ref1, length1), (ref2, length2), ...] .
    """
    logging.debug("get_bigwig_header_info: Getting ref:length info from reference fasta.\n")
    header = []
    with open(input_fasta) as fh:
        for record in SeqIO.parse(fh, "fasta"):
            header.append((record.id, len(record.seq)))
    return header

def write_bigwig_from_df(df, header, outname):
    """
    Function to write a bigwig file using a pandas dataframe from a bed file.

    :param df: Pandas dataframe object (created from bed file).
    :param header: List containing (ref name, length) information. (list of tuples)
    :param outname: Name of bigwig output file to write (OUT.bw).
    """
    logging.debug("write_bigwig_from_df: Writing bigwig file for '{}'.\n".format(outname))
    # first filter reference contigs to match those in bed file
    # get all unique ref contig names from bed
    chroms_present = list(df["chromosome"].unique())
    # header is a list of tuples, filter to keep only those present in bed
    # must also sort reference contigs by name
    filtered_header = sorted([x for x in header if x[0] in chroms_present], key=itemgetter(0))
    for i,j in filtered_header:
        logging.debug("\tHeader includes: '{}', '{}'.".format(i,j))
    # raise error if no reference contig names match
    if not filtered_header:
        logging.error("No reference contig names match between bed file and reference fasta!")
        raise ValueError("No reference contig names match between bed file and reference fasta!")

    # open bigwig object, enable writing mode (default is read only)
    bw = pyBigWig.open(outname, "w")
    # must add header to bigwig prior to writing entries
    bw.addHeader(filtered_header)
    # iterate over ref contig names
    for chrom, length in filtered_header:
        logging.debug("\tAdding entries for '{}'.".format(chrom))
        # subset dataframe by chromosome name
        temp_df = df[df["chromosome"] == chrom]
        logging.debug("\tNumber of entries = {:,}.".format(temp_df.shape[0]))
        # add entries in order specified for bigwig objects:
        # list of chr names: ["chr1", "chr1", "chr1"]
        # list of start coords: [1, 100, 125]
        # list of stop coords: ends=[6, 120, 126]
        # list of vals: values=[0.0, 1.0, 200.0]
        bw.addEntries(list(temp_df["chromosome"]),
                      list(temp_df["start"]),
                      ends=list(temp_df["stop"]),
                      values=list(temp_df["percent_modified"]))
        logging.debug("\tFinished entries for '{}'.\n".format(chrom))
    # close bigwig object
    bw.close()

def convert_bed_to_bigwig(bed_files, fasta, pileup_mode):
    """
    Write bigwig files for each output bed file.

    :param bed_files: List of output bed file names. (list)
    :param fasta: A path to reference fasta file. (str)
    :param pileup_mode: Site modification calling method. (str: "model", "count")
    """
    logging.info("convert_bed_to_bigwig: Converting {} bed files to bigwig files.\n".format(len(bed_files)))
    header = get_bigwig_header_info(fasta)
    for bed in bed_files:
        outname = "{}.bw".format(bed.split(".bed")[0])
        df = make_bed_df(bed, pileup_mode)
        write_bigwig_from_df(df, header, outname)

def main():
    args = get_args()
    setup_logging(args.output_label)

    if args.pileup_mode == "model":
        if args.model_dir == None:
            logging.error("Must supply a model to use when running model-based scoring!")
            raise ValueError("Must supply a model to use when running model-based scoring!")
        else:
            if not os.path.isdir(args.model_dir):
                logging.error("{} is not a valid directory path!".format(args.model_dir))
                raise ValueError("{} is not a valid directory path!".format(args.model_dir))

    print("\nChunking regions for multiprocessing.")
    regions_to_process = get_regions_to_process(args.bam, args.fasta, args.chunksize, args.modsites,
                                                args.pileup_mode, args.model_dir, args.min_mapq, args.hap_tag)

    print("Running multiprocessing on {:,} chunks.".format(len(regions_to_process)))
    bed_results = run_all_pileup_processing(regions_to_process, args.threads)

    print("Finished multiprocessing.\nWriting bed files.")
    bed_files = write_output_bed(args.output_label, args.modsites, args.min_coverage, bed_results)

    print("Writing bigwig files.")
    convert_bed_to_bigwig(bed_files, args.fasta, args.pileup_mode)

    print("Finished.\n")

if __name__ == '__main__':
    main()
