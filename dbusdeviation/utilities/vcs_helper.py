#!/usr/bin/python
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4
#
# Copyright © 2015 Collabora Ltd.
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Wrapper around dbus-interface-diff to integrate it with a VCS

This implements an API signature database in the project’s version control
system so that all users of the VCS can do API compatibility between all past
signed releases. Currently, only git is supported.
"""

import argparse
from contextlib import contextmanager
import os
import shutil
import subprocess
import sys
import tempfile


@contextmanager
def named_pipe():
    """Create and cleanup a named pipe in a temporary directory."""
    dirname = tempfile.mkdtemp()
    try:
        path = os.path.join(dirname, 'fifo')
        os.mkfifo(path)
        yield path
    finally:
        shutil.rmtree(dirname)


def _get_contents_of_file(args, tag, api_xml_file):
    """Get the git object ID of api_xml_file in the tag revision."""
    rev = subprocess.check_output([args.git, 'rev-parse',
                                   '--verify', '--quiet',
                                   '%s^{tag}:%s' % (tag, api_xml_file)])
    return rev.strip().decode('utf-8')


def _set_notes_for_ref(args, tag, api_xml_basename, notes):
    """Store the notes object ID as api_xml_basename in the tag revision."""
    with open(os.devnull, 'w') as dev_null:
        subprocess.check_output([args.git, 'notes',
                                 '--ref',
                                 'refs/%s/%s' %
                                 (args.dbus_api_git_refs, api_xml_basename),
                                 'add', '-C', notes, tag],
                                stderr=dev_null)


def _get_notes_filename_for_head(args, api_xml_basename):
    """Get the filename of api_xml_basename in the current working tree."""
    out = subprocess.check_output([args.git, 'ls-files',
                                   '*/%s' % api_xml_basename])
    return out.strip().decode('utf-8')


def _fetch_notes(args):
    """Fetch the latest API signature database from the remote."""
    subprocess.check_output([args.git, 'fetch', args.git_remote_origin,
                             'refs/%s/*:refs/%s/*' %
                             (args.dbus_api_git_refs, args.dbus_api_git_refs)])


def _push_notes(args):
    """Push the local API signature database to the remote."""
    subprocess.check_output([args.git, 'push', args.git_remote_origin,
                             'refs/' + args.dbus_api_git_refs + '/*'])


def _is_release(args, ref):
    """Check whether ref identifies a signed tag."""
    with open(os.devnull, 'w') as dev_null:
        code = subprocess.call([args.git, 'rev-parse', '--verify', ref],
                               stdout=dev_null, stderr=dev_null)
    return code == 0


def _get_latest_release(args):
    """Get the name of the latest signed tag."""
    tag_list = subprocess.check_output([args.git, 'rev-list',
                                        '--tags', '--max-count=1'])
    tag_list = tag_list.strip().decode('utf-8').split('\n')
    latest_tag = subprocess.check_output([args.git, 'describe',
                                          '--tags'] + tag_list)

    return latest_tag.strip().decode('utf-8')


def command_dist(args):
    """Store the current API signature against the latest signed tag."""
    # Get the latest git tag
    try:
        latest_tag = _get_latest_release(args)
    except subprocess.CalledProcessError:
        sys.stderr.write('error: Failed to find latest git tag: %s.')
        return 1

    # Store notes for each API file
    for api_xml_file in args.dbus_api_xml_files:
        try:
            notes = _get_contents_of_file(args, latest_tag, api_xml_file)
            api_xml_basename = os.path.basename(api_xml_file)
            subprocess.check_output([args.git, 'notes', '--ref',
                                     'refs/%s/%s' %
                                     (args.dbus_api_git_refs,
                                      api_xml_basename),
                                     'add', '-C', notes, latest_tag])

            sys.stdout.write('%s: Added note ‘%s’ for XML file ‘%s’\n' %
                             (latest_tag, notes, api_xml_basename))
        except subprocess.CalledProcessError:
            sys.stderr.write('error: Failed to store notes for API file ' +
                             '‘%s’ and git tag ‘%s’.\n' %
                             (api_xml_file, latest_tag))
            return 1

    # Push to the remote
    try:
        _push_notes(args)
    except subprocess.CalledProcessError:
        sys.stderr.write('error: Failed to push notes to remote ‘%s’.\n' %
                         args.git_remote_origin)
        return 1

    return 0


# pylint: disable=too-many-locals,too-many-branches,too-many-statements
def command_check(args):
    """
    Check for API differences between two tags.

    If old_ref is not specified, it defaults to the latest signed tag. If
    new_ref is not specified, it defaults to the current working tree.
    """
    if args.old_ref != '' and not _is_release(args, args.old_ref):
        sys.stderr.write('error: Invalid --old-ref ‘%s’\n' % args.old_ref)
        return 1

    if args.new_ref != '' and not _is_release(args, args.new_ref):
        sys.stderr.write('error: Invalid --new-ref ‘%s’\n' % args.new_ref)
        return 1

    try:
        _fetch_notes(args)
    except subprocess.CalledProcessError:
        # Continue anyway
        sys.stderr.write('error: Failed to fetch latest refs.\n')

    old_ref = args.old_ref
    new_ref = args.new_ref

    if old_ref == '':
        # Get the latest git tag
        try:
            old_ref = _get_latest_release(args)
        except subprocess.CalledProcessError:
            sys.stderr.write('error: Failed to find latest git tag.\n')
            return 1

    try:
        refs = subprocess.check_output([args.git, 'for-each-ref',
                                        '--format=%(refname)',
                                        'refs/%s' % args.dbus_api_git_refs])
        refs = refs.strip().decode('utf-8').split('\n')
    except subprocess.CalledProcessError:
        sys.stderr.write('error: Failed to get ref list.\n')
        return 1

    retval = 0

    for note_ref in refs:
        api_xml_basename = os.path.basename(note_ref)

        if args.silent:
            sys.stdout.write(' DIFF      %s\n' % api_xml_basename)
        else:
            sys.stdout.write('Comparing %s\n' % api_xml_basename)

        with named_pipe() as old_pipe_path, named_pipe() as new_pipe_path:
            old_notes_filename = old_pipe_path

            if new_ref == '':
                new_notes_filename = \
                    _get_notes_filename_for_head(args, api_xml_basename)
            else:
                new_notes_filename = new_pipe_path

            diff_command = ['dbus-interface-diff',
                            '--warnings', args.warnings,
                            old_notes_filename, new_notes_filename]
            old_notes_command = [args.git, 'notes',
                                 '--ref',
                                 'refs/%s/%s' %
                                 (args.dbus_api_git_refs, api_xml_basename),
                                 'show', old_ref]
            new_notes_command = [args.git, 'notes',
                                 '--ref',
                                 'refs/%s/%s' %
                                 (args.dbus_api_git_refs, api_xml_basename),
                                 'show', new_ref]

            diff_proc = subprocess.Popen(diff_command)

            with open(old_pipe_path, 'wb') as old_pipe, \
                    open(os.devnull, 'w') as dev_null:
                old_notes_proc = subprocess.Popen(old_notes_command,
                                                  stdout=old_pipe,
                                                  stderr=dev_null)

                if new_ref == '':
                    new_notes_proc = None

                    # Debug output. Roughly equivalent to `set -v`.
                    if not args.silent:
                        sys.stdout.write(('%s \\\n' +
                                          '   <(%s) \\\n' +
                                          '   `%s`\n') %
                                         (' '.join(diff_command[:-2]),
                                          ' '.join(old_notes_command),
                                          args.git + ' ls-files "*/' +
                                          api_xml_basename + '"'))
                else:
                    with open(new_pipe_path, 'wb') as new_pipe:
                        new_notes_proc = subprocess.Popen(new_notes_command,
                                                          stdout=new_pipe,
                                                          stderr=dev_null)

                    # Debug output. Roughly equivalent to `set -v`.
                    if not args.silent:
                        sys.stdout.write(('%s \\\n' +
                                          '   <(%s) \\\n' +
                                          '   <(%s)\n') %
                                         (' '.join(diff_command[:-2]),
                                          ' '.join(old_notes_command),
                                          ' '.join(new_notes_command)))

                old_notes_proc.communicate()
                old_notes_proc.wait()

                if new_notes_proc is not None:
                    new_notes_proc.wait()

            diff_proc.wait()

            # Output the status from the first failure
            if retval == 0 and diff_proc.returncode != 0:
                retval = diff_proc.returncode

    return retval


def command_install(args):
    """Set up the API signature database for all existing signed tags."""
    try:
        tag_list = subprocess.check_output([args.git, 'tag'])
        tag_list = tag_list.strip().decode('utf-8').split('\n')
    except subprocess.CalledProcessError:
        sys.stderr.write('error: Failed to get tag list.\n')
        return 1

    for tag in tag_list:
        outputted = False

        for api_xml_file in args.dbus_api_xml_files:
            api_xml_basename = os.path.basename(api_xml_file)

            try:
                notes = _get_contents_of_file(args, tag, api_xml_file)
            except subprocess.CalledProcessError:
                # Ignore it.
                notes = ''

            if notes == '':
                continue

            try:
                _set_notes_for_ref(args, tag, api_xml_basename, notes)

                sys.stdout.write('%s: Added note ‘%s’ for XML file ‘%s’\n' %
                                 (tag, notes, api_xml_basename))
                outputted = True
            except subprocess.CalledProcessError:
                # Ignore it
                pass

        if not outputted:
            sys.stdout.write('%s: Nothing to do\n' % tag)

    # Push the new refs
    try:
        _push_notes(args)
    except subprocess.CalledProcessError:
        sys.stderr.write('error: Failed to push notes to remote ‘%s’.\n' %
                         args.git_remote_origin)
        return 1

    return 0


def main():
    """Main helper implementation."""
    # Parse command line arguments.
    parser = argparse.ArgumentParser(
        description='Comparing D-Bus interface definitions')

    # Common arguments
    parser.add_argument('--silent', action='store_const', const=True,
                        default=False,
                        help='Silence all non-error output')
    # pylint: disable=bad-continuation
    parser.add_argument('--git', type=str, default='git', metavar='COMMAND',
                        help='Path to the git command, including extra ' +
                             'arguments')
    parser.add_argument('--git-remote', dest='git_remote_origin', type=str,
                        default='origin', metavar='REMOTE',
                        help='git remote to push notes to')
    # pylint: disable=bad-continuation
    parser.add_argument('--git-refs', dest='dbus_api_git_refs', type=str,
                        default='notes/dbus/api', metavar='REF-PATH',
                        help='Path beneath refs/ where the git notes will be' +
                             ' stored containing the API signatures database')

    subparsers = parser.add_subparsers()

    # dist command
    parser_dist = subparsers.add_parser('dist')
    parser_dist.add_argument('dbus_api_xml_files', metavar='API-FILE',
                             type=str, nargs='+',
                             help='D-Bus API XML file to check')
    parser_dist.set_defaults(func=command_dist)

    # check command
    parser_check = subparsers.add_parser('check')
    parser_check.add_argument('--diff-warnings', dest='warnings', type=str,
                              default='all',
                              help='Comma-separated list of warnings to ' +
                                   'enable when running dbus-interface-diff')
    # pylint: disable=bad-continuation
    parser_check.add_argument('old_ref', metavar='OLD-REF',
                              type=str, nargs='?', default='',
                              help='Old ref to compare; or empty for the '
                                   'latest signed tag')
    parser_check.add_argument('new_ref', metavar='NEW-REF',
                              type=str, nargs='?', default='',
                              help='New ref to compare; or empty for HEAD')
    parser_check.set_defaults(func=command_check)

    # install command
    parser_install = subparsers.add_parser('install')
    parser_install.add_argument('dbus_api_xml_files', metavar='API-FILE',
                                type=str, nargs='+',
                                help='D-Bus API XML file to install')
    parser_install.set_defaults(func=command_install)

    args = parser.parse_args()
    return args.func(args)

if __name__ == '__main__':
    main()