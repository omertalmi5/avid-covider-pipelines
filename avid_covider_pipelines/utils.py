import logging
import subprocess
import os
import stat
from dataflows import Flow, load, update_resource, sort_rows, printer
from dataflows.processors.dumpers.to_path import PathDumper
import datetime
from glob import glob
import hashlib
import json


HASH_BLOCKSIZE = 65536
HASH_IGNORE_FILENAME_ENDSWITH = [
    '.ipynb',
    '.py',
    '.pyc',
    '.css',
    '.bmp',
    '.zip',
    '.md',
]
HASH_IGNORE_FILENAME_CONTAINS = [
    'credentials',
    'google_api_key',
]


def subprocess_call_log(*args, log_file=None, **kwargs):
    if log_file:
        log_file = open(log_file, 'w')
    try:
        with subprocess.Popen(*args, **kwargs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            for line in iter(proc.stdout.readline, b''):
                line = line.decode().rstrip()
                logging.info(line)
                if log_file:
                    log_file.write(line + "\n")
            proc.wait()
            return proc.returncode
    finally:
        if log_file:
            log_file.close()


def load_if_exists(load_source, name, not_exists_rows, *args, **kwargs):
    if os.path.exists(load_source):
        return Flow(load(load_source, name, *args, **kwargs))
    else:
        return Flow(iter(not_exists_rows), update_resource(-1, name=name))


class dump_to_path(PathDumper):

    def write_file_to_output(self, filename, path):
        path = super(dump_to_path, self).write_file_to_output(filename, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        return path


def keep_last_runs_history(output_dir, run_callback, *callback_args, **callback_kwargs):
    run_row = {'start_time': datetime.datetime.now()}
    last_run_row = Flow(
        load_if_exists('%s/last_run/datapackage.json' % output_dir, 'last_run', [{}])
    ).results()[0][0][0]
    run_row = run_callback(last_run_row, run_row, *callback_args, **callback_kwargs)
    if run_row:
        Flow(
            iter([run_row]),
            update_resource(-1, name='last_run', path='last_run.csv', **{'dpp:streaming': True}),
            dump_to_path('%s/last_run' % output_dir)
        ).process()

    run_fields = set()
    if os.path.exists('%s/runs_history/datapackage.json' % output_dir):
        with open('%s/runs_history/datapackage.json' % output_dir) as f:
            datapackage = json.load(f)
        for f in datapackage['resources'][0]['schema']['fields']:
            run_fields.add(f['name'])

    if run_row:
        for k in run_row.keys():
            run_fields.add(k)

    def _get_runs_history():
        if os.path.exists('%s/runs_history/datapackage.json' % output_dir):
            for resource in Flow(
                load('%s/runs_history/datapackage.json' % output_dir),
            ).datastream().res_iter:
                for row in resource:
                    yield {k: row.get(k, '') for k in run_fields}
        if run_row:
            yield {k: run_row.get(k, '') for k in run_fields}

    Flow(
        _get_runs_history(),
        update_resource(-1, name='runs_history', path='runs_history', **{'dpp:streaming': True}),
        dump_to_path('%s/runs_history' % output_dir)
    ).process()

    return Flow(
        load('%s/runs_history/datapackage.json' % output_dir),
        sort_rows('{start_time}', reverse=True),
        printer(num_rows=10)
    )


def get_hash(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        buf = f.read(HASH_BLOCKSIZE)
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(HASH_BLOCKSIZE)
    return hasher.hexdigest()


def is_ignore_hash_filename(filename):
    for v in HASH_IGNORE_FILENAME_CONTAINS:
        if v in filename:
            return True
    for v in HASH_IGNORE_FILENAME_ENDSWITH:
        if filename.endswith(v):
            return True
    return False


def get_updated_files(hash_directory, glob_pattern, recursive, mtimes, sizes, hashes, updated_files_callback):
    for path in glob(os.path.join(hash_directory, glob_pattern), recursive=recursive):
        if os.path.isfile(path) and not is_ignore_hash_filename(path):
            if path not in mtimes or mtimes[path] != os.path.getmtime(path):
                filehash = get_hash(path)
                if path not in sizes or path not in hashes or sizes[path] != os.path.getsize(path) or hashes[path] != filehash:
                    row = {'path': path.replace(hash_directory + '/', ''), 'hash': filehash}
                    if updated_files_callback:
                        updated_files_callback(row)
                    yield row


def hash_updated_files(
        hash_directory, dump_to_path_name, run_callback,
        printer_num_rows=999, glob_pattern=None, recursive=True,
        run_callback_args=None, run_callback_kwargs=None,
        updated_files_callback=None
):
    mtimes = {}
    sizes = {}
    hashes = {}
    if glob_pattern is None:
        glob_pattern = "**" if recursive else "*"
    if run_callback_args is None:
        run_callback_args = []
    if run_callback_kwargs is None:
        run_callback_kwargs = {}
    for path in glob(os.path.join(hash_directory, glob_pattern), recursive=recursive):
        if os.path.isfile(path) and not is_ignore_hash_filename(path):
            mtimes[path] = os.path.getmtime(path)
            sizes[path] = os.path.getsize(path)
            hashes[path] = get_hash(path)
    run_callback(*run_callback_args, **run_callback_kwargs)
    return Flow(
        get_updated_files(hash_directory, glob_pattern, recursive, mtimes, sizes, hashes, updated_files_callback),
        update_resource(-1, name='updated_files', path='updated_files.csv', **{'dpp:streaming': True}),
        *([printer(num_rows=printer_num_rows)] if printer_num_rows > 0 else []),
        *([dump_to_path(dump_to_path_name)] if dump_to_path_name else [])
    )
