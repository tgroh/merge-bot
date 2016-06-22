"""Contains functions for creating and running mergers for various SCM systems.

The merger interacts with the underlying SCM to patch specific changes, verify
they work, and then submit.
"""
import abc
from multiprocessing import Queue
import os
from subprocess import check_call
import sys
import github_helper

APACHE_GIT = 'https://git-wip-us.apache.org/repos/asf/{repo}.git'
GITHUB_REPO_URL = 'https://github.com/{org}/{repo}.git'
TMP_DIR_FMT = '/tmp/{dir}'


def create_merger(config, work_queue):
    """create_merger creates a merger of the type specified in the config.

    Args:
        config: A dictionary containing repository configuration.
        work_queue: A Queue.Queue where new work items will be queued.
    Returns:
        Merger of type specified in configuration
    Raises:
        AttributeError: if passed an unsupported SCM type.
    """
    if config['scm_type'] == 'github':
        _print_flush('creating gitmerger')
        return GitMerger(config, work_queue)
    raise AttributeError('Unsupported SCM type: {}.'.format(config['scm_type']))


class Merger(object):
    """Merger is the base class for all mergers.
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, config, work_queue):
        _print_flush('merger init')
        self.config = config
        _print_flush('configged')
        #if not isinstance(work_queue, Queue):
        #    _print_flush('wasn\'t a queue')
        #    raise AttributeError('Expected work_queue to be of type '
        #                         'multiprocessing.Queue.')
        _print_flush('queueing')
        self.work_queue = work_queue
        _print_flush('done wit dat')

    @abc.abstractmethod
    def merge(self):
        """merge spins forever, merging things off of the work queue.
        """
        return


class GitMerger(Merger):
    """GitMerger merges Git pull requests.
    """

    def merge(self):
        """merge spins forever, merging things off of the work queue.

        Raises:
            AttributeError: if work items are not of type github_helper.GithubPR
        """
        _print_flush('in merge')
        while True:
            pr = self.work_queue.get()
            output_file = '{name}_pr_{pr_num}_merge_log.txt'.format(
                name=self.config['name'], pr_num=pr.get_num())
            with open(os.path.join('log', output_file), 'w') as log_file:
                sys.stdout = log_file
                if not isinstance(pr, github_helper.GithubPR.__class__):
                    raise AttributeError('Expected items in work_queue to be of'
                                         ' type github_helper.GithubPR')
                _print_flush('Starting merge process for #{}.'.format(
                    pr.get_num()))

                tmp_dir = TMP_DIR_FMT.format(dir='{}-{}'.format(
                    self.config['repository'], pr.get_num()))
                try:
                    _set_up(tmp_dir)
                except AssertionError:
                    pr.post_error('Setup of temp directory failed, try again.')
                    continue
                if self.merge_git_pr(pr, tmp_dir):
                    _print_flush('Merge concluded satisfactorily. Moving on.')
                else:
                    _print_flush('Merge did not conclude satisfactorily ('
                                 'reporting to github failed. Adding PR back to'
                                 ' queue to be tried again.')
                    self.work_queue.put(pr)
                _clean_up(tmp_dir)

    def merge_git_pr(self, pr, tmp_dir):
        """merge_git_pr merges git pull requests.

        Args:
            pr: The GithubPR to merge.
        Returns:
            True if merge was concluded satisfactorily (merged successfully,
            or failed due to supposed fault of the PR itself).
            False if failure was due to an environmental issue and should be
            retried.
        """
        branch = self.config['merge_branch']
        org = self.config['github_org']
        remote_name = 'apache'
        repo = self.config['repository']
        pr_vars = {
            'apache_url': APACHE_GIT.format(repo=repo),
            'branch': branch,
            'branch_path': '{}/{}'.format(remote_name, branch),
            'msg': 'This closes #{}'.format(pr.get_num()),
            'remote_name': remote_name,
            'repo': repo,
            'repo_url': GITHUB_REPO_URL.format(org=org, repo=repo),
            'pr_name': 'finish-pr-{}'.format(pr.get_num()),
            'pr_num': pr.get_num(),
        }
        try:
            cmds = [
                {
                    'cmd': 'git clone -b {branch} {repo_url}',
                    'desc': 'Clone',
                    'error': 'Clone failed. Please try again.',
                },
                {
                    'cmd': 'git remote add {remote_name} {apache_url}',
                    'desc': 'Add Remote',
                    'error': 'Failed to add remote. Please try again.',
                },
                {
                    'cmd': 'git remote rename origin github',
                    'desc': 'Rename Origin',
                    'error': 'Failed to rename origin. Please try again.',
                },
                {
                    'cmd': 'git config --local --add remote.github.fetch '
                           '"+refs/pull/*/head:refs/remotes/github/pr/*"',
                    'desc': 'Configure git fetch.',
                    'error': 'Failed to configure git fetch. Please try again.',
                    'shell': True,
                },
                {
                    'cmd': 'git fetch --all',
                    'desc': 'Fetch everything.',
                    'error': 'Fetch failed. Please try again.',
                },
                {
                    'cmd': 'git checkout -b {pr_name} github/pr/{pr_num}',
                    'desc': 'Checkout PR',
                    'error': 'Failed to check out PR. Please try again.',
                },
                {
                    'cmd': 'git rebase {branch_path}',
                    'desc': 'Rebase against target branch.',
                    'error': 'Automatic rebase failed. Please manually rebase '
                             'against {branch_path} and try again.',
                },
                {
                    'cmd': 'git checkout {branch_path}',
                    'desc': 'Check out target branch.',
                    'error': 'Failed to check out {branch_path}. Please try '
                             'again.',
                },
                {
                    'cmd': 'git merge --no-ff -m {msg} {pr_name}',
                    'desc': 'Merge PR',
                    'error': 'Merge was not successful. Please try again.',
                },
                {
                    'cmd': self.config['verification_command'],
                    'desc': 'Verifying PR',
                    'error': 'Verification failed. Please check the error log'
                             ' and try again.',
                },
                #{
                #    'cmd': 'git push {remote_name} HEAD:{branch}',
                #    'desc': 'Push to remote master.',
                #    'error': 'Remote push failed. Please try again.',
                #},
            ]
            for command in cmds:
                shell = True if 'shell' in command else False
                self.run(command['cmd'], pr_vars, command['desc'],
                         command['error'], pr, tmp_dir, shell=shell)
        except AssertionError as err:
            _print_flush(err)
            return True
        except EnvironmentError as err:
            _print_flush("Couldn't post comment to github. Leaving this on "
                         "the ")
            _print_flush(err)
            return False
        except Exception as err:
            _print_flush(err)
            return False
        try:
            pr.post_info('PR merge succeeded!')
            _print_flush('Merge for {pr_num} completed successfully.'.format(
                pr_num=pr.get_num()))
        except EnvironmentError as err:
            _print_flush(err)
            _print_flush('Pull Request success post failed. Moving on.')
        return True

    def run(self, cmd, fmt_dict, desc, error, pr, tmp_dir, shell=False):
        """Runs command.

        Args:
            cmd: Command to run.
            fmt_dict: Dictionary of parameters with which to format the
            command and other strings.
            desc: Description of command.
            error: Error to post to github if command fails.
            shell: Whether to run the command in shell mode.
            tmp_dir: Location in which to run command.
        Raises:
            AssertionError: If command was not successful.
            EnvironmentError: If PR comment couldn't be posted to Github.
        """
        cmd_fmt = cmd.format(**fmt_dict)
        _print_flush('Starting: {}.'.format(desc.format(**fmt_dict)))
        _print_flush('Running command: {}.'.format(cmd_fmt))
        try:
            check_call(cmd_fmt, cwd=tmp_dir, shell=shell)
        except:
            pr.post_error(error.format(**fmt_dict))
            raise AssertionError('Command "{}" failed.'.format(cmd_fmt))
        _print_flush('Finished: {}.'.format(desc.format(**fmt_dict)))


def _set_up(tmp_dir):
    """set_up creates a temp directory for use.

    Args:
        tmp_dir: Directory to create.
    Raises:
        AssertionError: If command was not successful.
    """
    try:
        check_call(['mkdir', tmp_dir])
    except:
        raise AssertionError('Setup failed.')


def _clean_up(tmp_dir):
    """_clean_up removes a temp directory after use.

    Args:
        tmp_dir: Directory to remove.
    """
    try:
        check_call(['rm', '-rf', tmp_dir])
    except:
        _print_flush('Cleanup failed.')


def _print_flush(msg):
    """_print_flush prints to stdout, then flushes immediately.

    Args:
        msg: the message to print.
    """
    print msg
    sys.stdout.flush()
