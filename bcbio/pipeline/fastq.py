"""Pipeline utilities to retrieve FASTQ formatted files for processing.
"""
import os
import shutil

from bcbio import bam, broad, utils
from bcbio.bam import fastq
from bcbio.distributed import objectstore
from bcbio.pipeline import alignment
from bcbio.pipeline import datadict as dd
from bcbio.utils import file_exists, safe_makedir, splitext_plus
from bcbio.provenance import do
from bcbio.distributed.transaction import file_transaction
from bcbio.ngsalign import alignprep


def get_fastq_files(data):
    """Retrieve fastq files for the given lane, ready to process.
    """
    assert "files" in data, "Did not find `files` in input; nothing to process"
    ready_files = []
    should_gzip = True

    # Bowtie does not accept gzipped fastq
    if 'bowtie' in data['reference'].keys():
        should_gzip = False
    for fname in data["files"]:
        if fname.endswith(".bam"):
            if _pipeline_needs_fastq(data["config"], data):
                ready_files = convert_bam_to_fastq(fname, data["dirs"]["work"],
                                                   data, data["dirs"], data["config"])
            else:
                ready_files = [fname]
        elif objectstore.is_remote(fname):
            ready_files.append(fname)
        # Trimming does quality conversion, so if not doing that, do an explicit conversion
        elif not(dd.get_trim_reads(data)) and dd.get_quality_format(data) != "standard":
            out_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(data), "fastq_convert"))
            ready_files.append(fastq.groom(fname, data, out_dir=out_dir))
        else:
            ready_files.append(fname)
    ready_files = [x for x in ready_files if x is not None]
    if should_gzip:
        out_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(data), "fastq"))
        ready_files = [_gzip_fastq(x, out_dir) for x in ready_files]
    for in_file in ready_files:
        if not objectstore.is_remote(in_file):
            assert os.path.exists(in_file), "%s does not exist." % in_file
    return ready_files

def _gzip_fastq(in_file, out_dir=None):
    """
    gzip a fastq file if it is not already gzipped, handling conversion
    from bzip to gzipped files
    """
    if fastq.is_fastq(in_file) and not objectstore.is_remote(in_file):
        if utils.is_bzipped(in_file):
            return _bzip_gzip(in_file, out_dir)
        elif not utils.is_gzipped(in_file):
            if out_dir:
                gzipped_file = os.path.join(out_dir, os.path.basename(in_file) + ".gz")
            else:
                gzipped_file = in_file + ".gz"
            if file_exists(gzipped_file):
                return gzipped_file
            message = "gzipping {in_file} to {gzipped_file}.".format(
                in_file=in_file, gzipped_file=gzipped_file)
            with file_transaction(gzipped_file) as tx_gzipped_file:
                do.run("gzip -c {in_file} > {tx_gzipped_file}".format(**locals()),
                       message)
            return gzipped_file
    return in_file

def _bzip_gzip(in_file, out_dir=None):
    """
    convert from bz2 to gz
    """
    if not utils.is_bzipped(in_file):
        return in_file
    base, _ = os.path.splitext(in_file)
    if out_dir:
        gzipped_file = os.path.join(out_dir, os.path.basename(base) + ".gz")
    else:
        gzipped_file = base + ".gz"
    if (fastq.is_fastq(base) and not objectstore.is_remote(in_file)):
        if file_exists(gzipped_file):
            return gzipped_file
        message = "gzipping {in_file} to {gzipped_file}.".format(
            in_file=in_file, gzipped_file=gzipped_file)
        with file_transaction(gzipped_file) as tx_gzipped_file:
            do.run("bunzip2 -c {in_file} | gzip > {tx_gzipped_file}".format(**locals()), message)
        return gzipped_file
    return in_file

def _pipeline_needs_fastq(config, data):
    """Determine if the pipeline can proceed with a BAM file, or needs fastq conversion.
    """
    aligner = config["algorithm"].get("aligner")
    support_bam = aligner in alignment.metadata.get("support_bam", [])
    return aligner and not support_bam


def convert_bam_to_fastq(in_file, work_dir, data, dirs, config):
    """Convert BAM input file into FASTQ files.
    """
    return alignprep.prep_fastq_inputs([in_file], data)

def merge(files, out_file, config):
    """merge smartly fastq files. It recognizes paired fastq files."""
    pair1 = [fastq_file[0] for fastq_file in files]
    if len(files[0]) > 1:
        path = splitext_plus(out_file)
        pair1_out_file = path[0] + "_R1" + path[1]
        pair2 = [fastq_file[1] for fastq_file in files]
        pair2_out_file = path[0] + "_R2" + path[1]
        _merge_list_fastqs(pair1, pair1_out_file, config)
        _merge_list_fastqs(pair2, pair2_out_file, config)
        return [pair1_out_file, pair2_out_file]
    else:
        return _merge_list_fastqs(pair1, out_file, config)

def _merge_list_fastqs(files, out_file, config):
    """merge list of fastq files into one"""
    if not all(map(fastq.is_fastq, files)):
        raise ValueError("Not all of the files to merge are fastq files: %s " % (files))
    assert all(map(utils.file_exists, files)), ("Not all of the files to merge "
                                                "exist: %s" % (files))
    if not file_exists(out_file):
        files = [_gzip_fastq(fn) for fn in files]
        if len(files) == 1:
            # os.symlink(files[0], out_file)
            shutil.move(files[0], out_file)
            return out_file
        with file_transaction(out_file) as file_txt_out:
            files_str = " ".join(list(files))
            cmd = "cat {files_str} > {file_txt_out}".format(**locals())
            do.run(cmd, "merge fastq files %s" % files)
    return out_file
