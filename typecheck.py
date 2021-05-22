"""This script runs mypy on all py files in the given directory.

Author: Shining (Fred) Lou
"""
import os
import sys
import subprocess
from typing import List, Tuple
from add_reveal_locals_typed import process_reveal_locals


def get_src_file_list(dir: str) -> List[str]:
    """ Get all py files from the given directories
    """
    res = []
    excepted_files = ['typecheck.py', 'add_reveal_locals_typed.py']
    for root, dirs, files in os.walk(dir):
        dirs.sort()
        for file_name in sorted(files):
            if file_name[-3:] != '.py': continue
            full_path = os.path.join(root, file_name)
            excepted = False
            for excepted_file in excepted_files:
                if os.path.samefile(full_path, excepted_file):
                    excepted = True
                    break
            if not excepted:
                res.append(full_path)
    return res


def run_mypy(files: List[str] = []):
    """ Call mypy given list of files, packages, modules and arguments
    Output: stdout, stderr
    """

    args = ['mypy', '--follow-imports', 'silent', '--ignore-missing-imports', '--check-untyped-defs', '--sqlite-cache',
            '--show-error-codes']
    command = args + files
    # print(command)
    # subprocess.run(command)
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = []
    for line in proc.stdout:
        log.append(line.decode('utf-8'))
        sys.stdout.write(line.decode('utf-8'))
    return log


def process_duplicates(files: List[str]):
    file_name_set = set()
    base_file_list = []
    duplicate_file_list = []
    for file in files:
        file_split = file.split("/")
        file_name = file_split[len(file_split) - 1]
        if file_name not in file_name_set:
            file_name_set.add(file_name)
            base_file_list.append(file)
        else:
            duplicate_file_list.append(file)
    return base_file_list,duplicate_file_list


def main() -> None:
    process_reveal_locals()

    src_files = get_src_file_list('.')
    base_file_list, duplicate_file_list = process_duplicates(src_files)

    python_version = '{}.{}'.format(sys.version_info.major, sys.version_info.minor)
    os.mkdir('mypy_test_cache')

    log_file = open('mypy_test_report.txt', 'w')

    output = run_mypy(base_file_list)
    os.rename('./.mypy_cache/{}/cache.db'.format(python_version), './mypy_test_cache/main_cache.db')

    log_file.writelines(output)

    i = 0
    while len(duplicate_file_list) > 0:
        base_file_list, duplicate_file_list = process_duplicates(duplicate_file_list)


    # for dup_file in duplicate_file_list:
        output = run_mypy(base_file_list)
        os.rename('./.mypy_cache/{}/cache.db'.format(python_version), './mypy_test_cache/duplicate_cache({}).db'.format(i))

        log_file.writelines(output)

        i += 1
    
    log_file.close()


if __name__ == '__main__':
    main()