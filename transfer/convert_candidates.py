# some candidate kernels downloaded are not actually ipython notebooks/python scripts (despite Kaggle filtering)
# so we convert if necessary and then make sure they can be parsed with python3, if not convert

from argparse import ArgumentParser
import ast
import logging
import glob
import os
import shutil
import subprocess
import tempfile


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(message)s",
)
log = logging.getLogger(__name__)


def get_basename(_path, with_ext=True):
    name = os.path.basename(_path)
    if not with_ext:
        name = '.'.join(name.split('.')[:-1])
    return name

def get_ipython_notebooks(input_dir):
    pattern = os.path.join(input_dir, '*.[xi]pynb')
    return glob.glob(pattern)

def get_py_scripts(input_dir):
    pattern = os.path.join(input_dir, '*.py')
    return glob.glob(pattern)

def convert_notebook_to_script(notebook_path, output_path):
    command = ['jupyter', 'nbconvert', '--to', 'python', notebook_path, '--output', output_path]
    subprocess.call(command)

def check_can_parse(script_path):
    try:
        with open(script_path, 'r') as f:
            ast.parse(f.read())
        return True
    except SyntaxError:
        return False

def convert_2_to_3(script_path, output_dir):
    command = ['2to3', '--write', '-n', '--output-dir=%s' % output_dir, script_path]
    subprocess.call(command)
    new_path = os.path.join(output_dir, get_basename(script_path))
    return new_path

def filter_scripts(input_dir, output_dir=None):
    if output_dir is None:
        output_dir = os.path.joins(input_dir, 'parsed/')

    if not os.path.exists(output_dir):
        os.mkdir(output_dir)

    # temporary directory to write converted files when necessary
    temp_dir = tempfile.mkdtemp(dir=input_dir)

    for script in get_py_scripts(input_dir):
        parseable = check_can_parse(script)
        if not parseable:
            log.info('{} script fails to parse, attempting to convert to py3'.format(script))
            script = convert_2_to_3(script, temp_dir)
            # check again in case the conversion didn't work
            if os.path.exists(script):
                parseable = check_can_parse(script)

        if parseable:
            log.info('Copying {0} to {1}'.format(script, output_dir))
            new_path = os.path.join(output_dir, get_basename(script))
            shutil.copy(script, new_path)

    # clean up temporary contents (everything needed would have been )
    log.debug('Removing temporary folder (for 2to3 conversions)')
    shutil.rmtree(temp_dir)
    return output_dir

def convert_notebooks(input_dir, output_dir=None):
    if output_dir is None:
        output_dir = os.path.join(input_dir, 'converted_notebooks')

    if not os.path.exists(output_dir):
        os.mkdir(output_dir)

    for notebook in get_ipython_notebooks(input_dir):
        log.info('Converting notebook {} to python script'.format(notebook))
        new_name = get_basename(notebook, with_ext=False) + '.py'
        new_path = os.path.join(output_dir, new_name)
        convert_notebook_to_script(notebook, new_path)

    return output_dir

def filter_candidates(input_dir, parsed_dir, converted_dir):
    # *.py scripts
    os.path.join(input_dir, parsed_dir)
    filter_scripts(input_dir, parsed_dir)

    # *.ipynb -> *.py
    converted_dir = os.path.join(input_dir, converted_dir)
    convert_notebooks(input_dir, converted_dir)
    # converted_notebooks/*.py -> parsed/*.py
    filter_scripts(converted_dir, parsed_dir)

    n = len(os.listdir(parsed_dir))
    print("Filtered down to {} candidates that can be parsed with Python 3.*".format(n))

def main(args):
    filter_candidates(args.input_dir, args.parsed_dir, args.converted_dir)

if __name__ == "__main__":
    parser = ArgumentParser(description='Convert ipython notebook kernels to py scripts and filter based on parsing from Python 3.*')
    parser.add_argument('input_dir', type=str, help='Directory containing Kaggle kernels')
    parser.add_argument('-o', '--parsed_dir', type=str, help='Directory to store parsed kernels', default='parsed_kernels')
    parser.add_argument('-c', '--converted_dir', type=str, help='Directory to store converted notebooks', default='converted_notebooks')

    args = parser.parse_args()
    try:
        main(args)
    except Exception as err:
        import pdb
        pdb.post_mortem()


