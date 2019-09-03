# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2019
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import logging
import os
import re
import subprocess
import textwrap

import fasteners
from github import Github

from qiskit_bot import git

LOG = logging.getLogger(__name__)


def _run_reno(local_path, version_number):
    try:
        res = subprocess.run(['reno', 'report', '--version', version_number],
                             cwd=local_path, capture_output=True, check=True)
        return res.stdout
    except subprocess.CalledProcessError:
        LOG.exception('reno report failed')
        return None


def bump_meta(meta_repo, repo, version_number, conf, reno=None):

    version_number_pieces = version_number.split('.')
    meta_version = git.get_latest_tag(repo)
    meta_version_pieces = meta_version.split('.')
    if version_number_pieces[2] == 0:
        new_meta_version = '%s.%s.%s' % (meta_version_pieces[0],
                                         meta_version_pieces[1] + 1, 0)
    else:
        new_meta_version = '%s.%s.%s' % (meta_version_pieces[0],
                                         meta_version_pieces[1],
                                         meta_version_pieces[2] + 1)
    package_name = repo.repo_name.split('/')[1]
    pulls = meta_repo.gh_repo.get_pulls(state='open')
    setup_py_path = os.path.join(repo.local_path, 'setup.py')
    title = 'Bump Meta'
    requirements_str = package_name + '==' + version_number
    bump_pr = None
    for pull in pulls:
        if pull.title == title:
            bump_pr = pull

            break
    else:
        git.create_branch('bump_meta', 'origin/master', meta_repo)

    git.checkout_ref('bump_meta')
    with open(setup_py_path, 'w') as fd:
        for line in fd:
            if package_name in line:
                old_version = re.match(package_name + '==(.*)\n', line)[1]
                line.replace(package_name + '==' + old_version,
                             requirements_str)
            elif 'version=' in line:
                old_version = re.match('version=(.*)', line)[1]
                if old_version != new_meta_version:
                    line.replace('version=%s' % old_version,
                                 'version=%s' % new_meta_version)
    body = """
Bump the meta repo version to include:

%s

""" % requirements_str
    git.create_git_commit_for_all(
        meta_repo, 'Bump version for %s' % requirements_str)
    if not bump_pr:
        meta_repo.gh_repo.create_pull(title, base='master', head='bump_meta',
                                      body=body)


def _generate_changelog(repo, log_string, categories):
    git_log = git.get_git_log(repo, log_string)
    if not git_log:
        return None
    git_summaries = []
    pr_regex = re.compile(r'^.*\((.*)\)')
    for line in git_log:
        pieces = line.split(' ')
        if 'tag:' in line:
            summary = ' '.join(pieces[3:])
            pr = pr_regex.match(summary)[1][1:]
        else:
            summary = ' '.join(pieces[1:])
            pr = pr_regex.match(summary)[1][1:]
        git_summaries.append((summary, pr))
    changelog_dict = {x: [] for x in categories.keys()}
    for summary, pr in git_summaries:
        labels = [x.name for x in repo.gh_repo.get_pull(int(pr)).labels]
        for label in labels:
            if label in changelog_dict:
                changelog_dict[label].append(summary)
    changelog = "# Changelog\n"
    for label in changelog_dict:
        changelog.append('## %s\n' % categories[label])
        for pr in changelog_dict[label]:
            entry = textwrap.wrap('-   %s' % pr, 79)
            entry += '\n'
        changelog.append('\n')
    return changelog


def create_github_release(repo, log_string, version_number, categories):
    changelog = _generate_changelog(repo, log_string, categories)
    release_name = repo.name + ' ' + version_number
    repo.gh_repo.create_git_release(version_number, release_name, changelog)


def finish_release(version_number, repo, conf, meta_repo):
    """Do the post tag release processes."""
    working_dir = conf.get('working_dir')
    lock_dir = os.path.join(working_dir, 'lock')
    gh_session = Github(conf['api_key'])
    repo_config = conf['repos'][repo.repo_name]
    meta_repo = gh_session.get_repo(meta_repo)
    reno_notes = None
    version_number_pieces = version_number.split('.')
    branch_number = '.'.join(version_number_pieces[:2])
    reno_notes = None
    with fasteners.InterProcessLock(os.path.join(lock_dir, repo.name)):
        if repo_config['reno']:
            reno_notes = _run_reno(repo.local_path, version_number)
        if repo_config['branch_on_release']:
            branch_name = 'stable/%s' % branch_number
            repo_branches = [x.name for x in repo.gh_repo.get_branches()]
            if version_number_pieces[2] == 0 and \
                    branch_name not in repo_branches:
                git.create_branch(branch_name, version_number, repo, push=True)
        # If a patch release log between 0.A.X..0.A.X-1
        if version_number_pieces[2] > 0:
            log_string = '%s...%s' % (
                version_number,
                '.'.join(
                    version_number_pieces[:2] + [
                        version_number_pieces[2] - 1]))
        # If a minor release log between 0.X.0..0.X-1.0
        else:
            log_string = '%s...%s' % (
                version_number,
                '.'.join(
                    version_number_pieces[0] + [
                        version_number_pieces[1] - 1, 0]))

        create_github_release(repo, log_string, version_number,
                              repo_config['changelog_categories'])

    with fasteners.InterProcessLock(os.path.join(lock_dir, meta_repo.name)):
        bump_meta(meta_repo, repo, version_number, conf, reno_notes)
        git.checkout_master(repo, pull=True)