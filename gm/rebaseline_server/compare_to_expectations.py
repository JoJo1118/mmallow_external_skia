#!/usr/bin/python

"""
Copyright 2013 Google Inc.

Use of this source code is governed by a BSD-style license that can be
found in the LICENSE file.

Repackage expected/actual GM results as needed by our HTML rebaseline viewer.
"""

# System-level imports
import argparse
import fnmatch
import json
import logging
import os
import re
import sys
import time

# Imports from within Skia
#
# TODO(epoger): Once we move the create_filepath_url() function out of
# download_actuals into a shared utility module, we won't need to import
# download_actuals anymore.
#
# We need to add the 'gm' directory, so that we can import gm_json.py within
# that directory.  That script allows us to parse the actual-results.json file
# written out by the GM tool.
# Make sure that the 'gm' dir is in the PYTHONPATH, but add it at the *end*
# so any dirs that are already in the PYTHONPATH will be preferred.
PARENT_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
GM_DIRECTORY = os.path.dirname(PARENT_DIRECTORY)
TRUNK_DIRECTORY = os.path.dirname(GM_DIRECTORY)
if GM_DIRECTORY not in sys.path:
  sys.path.append(GM_DIRECTORY)
import download_actuals
import gm_json
import imagediffdb
import imagepair
import imagepairset
import results

EXPECTATION_FIELDS_PASSED_THRU_VERBATIM = [
    results.KEY__EXPECTATIONS__BUGS,
    results.KEY__EXPECTATIONS__IGNOREFAILURE,
    results.KEY__EXPECTATIONS__REVIEWED,
]

IMAGEPAIR_SET_DESCRIPTIONS = ('expected image', 'actual image')

DEFAULT_ACTUALS_DIR = '.gm-actuals'
DEFAULT_EXPECTATIONS_DIR = os.path.join(TRUNK_DIRECTORY, 'expectations', 'gm')
DEFAULT_GENERATED_IMAGES_ROOT = os.path.join(
    PARENT_DIRECTORY, '.generated-images')


class Results(object):
  """ Loads actual and expected GM results into an ImagePairSet.

  Loads actual and expected results from all builders, except for those skipped
  by _ignore_builder().

  Once this object has been constructed, the results (in self._results[])
  are immutable.  If you want to update the results based on updated JSON
  file contents, you will need to create a new Results object."""

  def __init__(self, actuals_root=DEFAULT_ACTUALS_DIR,
               expected_root=DEFAULT_EXPECTATIONS_DIR,
               generated_images_root=DEFAULT_GENERATED_IMAGES_ROOT,
               diff_base_url=None):
    """
    Args:
      actuals_root: root directory containing all actual-results.json files
      expected_root: root directory containing all expected-results.json files
      generated_images_root: directory within which to create all pixel diffs;
          if this directory does not yet exist, it will be created
      diff_base_url: base URL within which the client should look for diff
          images; if not specified, defaults to a "file:///" URL representation
          of generated_images_root
    """
    time_start = int(time.time())
    self._image_diff_db = imagediffdb.ImageDiffDB(generated_images_root)
    self._diff_base_url = (
        diff_base_url or
        download_actuals.create_filepath_url(generated_images_root))
    self._actuals_root = actuals_root
    self._expected_root = expected_root
    self._load_actual_and_expected()
    self._timestamp = int(time.time())
    logging.info('Results complete; took %d seconds.' %
                 (self._timestamp - time_start))

  def get_timestamp(self):
    """Return the time at which this object was created, in seconds past epoch
    (UTC).
    """
    return self._timestamp

  def edit_expectations(self, modifications):
    """Edit the expectations stored within this object and write them back
    to disk.

    Note that this will NOT update the results stored in self._results[] ;
    in order to see those updates, you must instantiate a new Results object
    based on the (now updated) files on disk.

    Args:
      modifications: a list of dictionaries, one for each expectation to update:

         [
           {
             imagepair.KEY__EXPECTATIONS_DATA: {
               results.KEY__EXPECTATIONS__BUGS: [123, 456],
               results.KEY__EXPECTATIONS__IGNOREFAILURE: false,
               results.KEY__EXPECTATIONS__REVIEWED: true,
             },
             imagepair.KEY__EXTRA_COLUMN_VALUES: {
               results.KEY__EXTRACOLUMN__BUILDER: 'Test-Mac10.6-MacMini4.1-GeForce320M-x86-Debug',
               results.KEY__EXTRACOLUMN__CONFIG: '8888',
               results.KEY__EXTRACOLUMN__TEST: 'bigmatrix',
             },
             results.KEY__NEW_IMAGE_URL: 'bitmap-64bitMD5/bigmatrix/10894408024079689926.png',
           },
           ...
         ]

    """
    expected_builder_dicts = Results._read_dicts_from_root(self._expected_root)
    for mod in modifications:
      image_name = results.IMAGE_FILENAME_FORMATTER % (
          mod[imagepair.KEY__EXTRA_COLUMN_VALUES]
             [results.KEY__EXTRACOLUMN__TEST],
          mod[imagepair.KEY__EXTRA_COLUMN_VALUES]
             [results.KEY__EXTRACOLUMN__CONFIG])
      _, hash_type, hash_digest = gm_json.SplitGmRelativeUrl(
          mod[results.KEY__NEW_IMAGE_URL])
      allowed_digests = [[hash_type, int(hash_digest)]]
      new_expectations = {
          gm_json.JSONKEY_EXPECTEDRESULTS_ALLOWEDDIGESTS: allowed_digests,
      }
      for field in EXPECTATION_FIELDS_PASSED_THRU_VERBATIM:
        value = mod[imagepair.KEY__EXPECTATIONS_DATA].get(field)
        if value is not None:
          new_expectations[field] = value
      builder_dict = expected_builder_dicts[
          mod[imagepair.KEY__EXTRA_COLUMN_VALUES]
             [results.KEY__EXTRACOLUMN__BUILDER]]
      builder_expectations = builder_dict.get(gm_json.JSONKEY_EXPECTEDRESULTS)
      if not builder_expectations:
        builder_expectations = {}
        builder_dict[gm_json.JSONKEY_EXPECTEDRESULTS] = builder_expectations
      builder_expectations[image_name] = new_expectations
    Results._write_dicts_to_root(expected_builder_dicts, self._expected_root)

  def get_results_of_type(self, results_type):
    """Return results of some/all tests (depending on 'results_type' parameter).

    Args:
      results_type: string describing which types of results to include; must
          be one of the RESULTS_* constants

    Results are returned in a dictionary as output by ImagePairSet.as_dict().
    """
    return self._results[results_type]

  def get_packaged_results_of_type(self, results_type, reload_seconds=None,
                                   is_editable=False, is_exported=True):
    """ Package the results of some/all tests as a complete response_dict.

    Args:
      results_type: string indicating which set of results to return;
          must be one of the RESULTS_* constants
      reload_seconds: if specified, note that new results may be available once
          these results are reload_seconds old
      is_editable: whether clients are allowed to submit new baselines
      is_exported: whether these results are being made available to other
          network hosts
    """
    response_dict = self._results[results_type]
    time_updated = self.get_timestamp()
    response_dict[results.KEY__HEADER] = {
        results.KEY__HEADER__SCHEMA_VERSION: (
            results.REBASELINE_SERVER_SCHEMA_VERSION_NUMBER),

        # Timestamps:
        # 1. when this data was last updated
        # 2. when the caller should check back for new data (if ever)
        results.KEY__HEADER__TIME_UPDATED: time_updated,
        results.KEY__HEADER__TIME_NEXT_UPDATE_AVAILABLE: (
            (time_updated+reload_seconds) if reload_seconds else None),

        # The type we passed to get_results_of_type()
        results.KEY__HEADER__TYPE: results_type,

        # Hash of dataset, which the client must return with any edits--
        # this ensures that the edits were made to a particular dataset.
        results.KEY__HEADER__DATAHASH: str(hash(repr(
            response_dict[imagepairset.KEY__IMAGEPAIRS]))),

        # Whether the server will accept edits back.
        results.KEY__HEADER__IS_EDITABLE: is_editable,

        # Whether the service is accessible from other hosts.
        results.KEY__HEADER__IS_EXPORTED: is_exported,
    }
    return response_dict

  @staticmethod
  def _ignore_builder(builder):
    """Returns True if we should ignore expectations and actuals for a builder.

    This allows us to ignore builders for which we don't maintain expectations
    (trybots, Valgrind, ASAN, TSAN), and avoid problems like
    https://code.google.com/p/skia/issues/detail?id=2036 ('rebaseline_server
    produces error when trying to add baselines for ASAN/TSAN builders')

    Args:
      builder: name of this builder, as a string

    Returns:
      True if we should ignore expectations and actuals for this builder.
    """
    return (builder.endswith('-Trybot') or
            ('Valgrind' in builder) or
            ('TSAN' in builder) or
            ('ASAN' in builder))

  @staticmethod
  def _read_dicts_from_root(root, pattern='*.json'):
    """Read all JSON dictionaries within a directory tree.

    Args:
      root: path to root of directory tree
      pattern: which files to read within root (fnmatch-style pattern)

    Returns:
      A meta-dictionary containing all the JSON dictionaries found within
      the directory tree, keyed by the builder name of each dictionary.

    Raises:
      IOError if root does not refer to an existing directory
    """
    if not os.path.isdir(root):
      raise IOError('no directory found at path %s' % root)
    meta_dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
      for matching_filename in fnmatch.filter(filenames, pattern):
        builder = os.path.basename(dirpath)
        if Results._ignore_builder(builder):
          continue
        fullpath = os.path.join(dirpath, matching_filename)
        meta_dict[builder] = gm_json.LoadFromFile(fullpath)
    return meta_dict

  @staticmethod
  def _create_relative_url(hashtype_and_digest, test_name):
    """Returns the URL for this image, relative to GM_ACTUALS_ROOT_HTTP_URL.

    If we don't have a record of this image, returns None.

    Args:
      hashtype_and_digest: (hash_type, hash_digest) tuple, or None if we
          don't have a record of this image
      test_name: string; name of the GM test that created this image
    """
    if not hashtype_and_digest:
      return None
    return gm_json.CreateGmRelativeUrl(
        test_name=test_name,
        hash_type=hashtype_and_digest[0],
        hash_digest=hashtype_and_digest[1])

  @staticmethod
  def _write_dicts_to_root(meta_dict, root, pattern='*.json'):
    """Write all per-builder dictionaries within meta_dict to files under
    the root path.

    Security note: this will only write to files that already exist within
    the root path (as found by os.walk() within root), so we don't need to
    worry about malformed content writing to disk outside of root.
    However, the data written to those files is not double-checked, so it
    could contain poisonous data.

    Args:
      meta_dict: a builder-keyed meta-dictionary containing all the JSON
                 dictionaries we want to write out
      root: path to root of directory tree within which to write files
      pattern: which files to write within root (fnmatch-style pattern)

    Raises:
      IOError if root does not refer to an existing directory
      KeyError if the set of per-builder dictionaries written out was
               different than expected
    """
    if not os.path.isdir(root):
      raise IOError('no directory found at path %s' % root)
    actual_builders_written = []
    for dirpath, dirnames, filenames in os.walk(root):
      for matching_filename in fnmatch.filter(filenames, pattern):
        builder = os.path.basename(dirpath)
        if Results._ignore_builder(builder):
          continue
        per_builder_dict = meta_dict.get(builder)
        if per_builder_dict is not None:
          fullpath = os.path.join(dirpath, matching_filename)
          gm_json.WriteToFile(per_builder_dict, fullpath)
          actual_builders_written.append(builder)

    # Check: did we write out the set of per-builder dictionaries we
    # expected to?
    expected_builders_written = sorted(meta_dict.keys())
    actual_builders_written.sort()
    if expected_builders_written != actual_builders_written:
      raise KeyError(
          'expected to write dicts for builders %s, but actually wrote them '
          'for builders %s' % (
              expected_builders_written, actual_builders_written))

  def _load_actual_and_expected(self):
    """Loads the results of all tests, across all builders (based on the
    files within self._actuals_root and self._expected_root),
    and stores them in self._results.
    """
    logging.info('Reading actual-results JSON files from %s...' %
                 self._actuals_root)
    actual_builder_dicts = Results._read_dicts_from_root(self._actuals_root)
    logging.info('Reading expected-results JSON files from %s...' %
                 self._expected_root)
    expected_builder_dicts = Results._read_dicts_from_root(self._expected_root)

    all_image_pairs = imagepairset.ImagePairSet(
        descriptions=IMAGEPAIR_SET_DESCRIPTIONS,
        diff_base_url=self._diff_base_url)
    failing_image_pairs = imagepairset.ImagePairSet(
        descriptions=IMAGEPAIR_SET_DESCRIPTIONS,
        diff_base_url=self._diff_base_url)

    all_image_pairs.ensure_extra_column_values_in_summary(
        column_id=results.KEY__EXTRACOLUMN__RESULT_TYPE, values=[
            results.KEY__RESULT_TYPE__FAILED,
            results.KEY__RESULT_TYPE__FAILUREIGNORED,
            results.KEY__RESULT_TYPE__NOCOMPARISON,
            results.KEY__RESULT_TYPE__SUCCEEDED,
        ])
    failing_image_pairs.ensure_extra_column_values_in_summary(
        column_id=results.KEY__EXTRACOLUMN__RESULT_TYPE, values=[
            results.KEY__RESULT_TYPE__FAILED,
            results.KEY__RESULT_TYPE__FAILUREIGNORED,
            results.KEY__RESULT_TYPE__NOCOMPARISON,
        ])

    builders = sorted(actual_builder_dicts.keys())
    num_builders = len(builders)
    builder_num = 0
    for builder in builders:
      builder_num += 1
      logging.info('Generating pixel diffs for builder #%d of %d, "%s"...' %
                   (builder_num, num_builders, builder))
      actual_results_for_this_builder = (
          actual_builder_dicts[builder][gm_json.JSONKEY_ACTUALRESULTS])
      for result_type in sorted(actual_results_for_this_builder.keys()):
        results_of_this_type = actual_results_for_this_builder[result_type]
        if not results_of_this_type:
          continue
        for image_name in sorted(results_of_this_type.keys()):
          (test, config) = results.IMAGE_FILENAME_RE.match(image_name).groups()
          actual_image_relative_url = Results._create_relative_url(
              hashtype_and_digest=results_of_this_type[image_name],
              test_name=test)

          # Default empty expectations; overwrite these if we find any real ones
          expectations_per_test = None
          expected_image_relative_url = None
          expectations_dict = None
          try:
            expectations_per_test = (
                expected_builder_dicts
                [builder][gm_json.JSONKEY_EXPECTEDRESULTS][image_name])
            # TODO(epoger): assumes a single allowed digest per test, which is
            # fine; see https://code.google.com/p/skia/issues/detail?id=1787
            expected_image_hashtype_and_digest = (
                expectations_per_test
                [gm_json.JSONKEY_EXPECTEDRESULTS_ALLOWEDDIGESTS][0])
            expected_image_relative_url = Results._create_relative_url(
                hashtype_and_digest=expected_image_hashtype_and_digest,
                test_name=test)
            expectations_dict = {}
            for field in EXPECTATION_FIELDS_PASSED_THRU_VERBATIM:
              expectations_dict[field] = expectations_per_test.get(field)
          except (KeyError, TypeError):
            # There are several cases in which we would expect to find
            # no expectations for a given test:
            #
            # 1. result_type == NOCOMPARISON
            #   There are no expectations for this test yet!
            #
            # 2. alternate rendering mode failures (e.g. serialized)
            #   In cases like
            #   https://code.google.com/p/skia/issues/detail?id=1684
            #   ('tileimagefilter GM test failing in serialized render mode'),
            #   the gm-actuals will list a failure for the alternate
            #   rendering mode even though we don't have explicit expectations
            #   for the test (the implicit expectation is that it must
            #   render the same in all rendering modes).
            #
            # Don't log type 1, because it is common.
            # Log other types, because they are rare and we should know about
            # them, but don't throw an exception, because we need to keep our
            # tools working in the meanwhile!
            if result_type != results.KEY__RESULT_TYPE__NOCOMPARISON:
              logging.warning('No expectations found for test: %s' % {
                  results.KEY__EXTRACOLUMN__BUILDER: builder,
                  results.KEY__EXTRACOLUMN__RESULT_TYPE: result_type,
                  'image_name': image_name,
                  })

          # If this test was recently rebaselined, it will remain in
          # the 'failed' set of actuals until all the bots have
          # cycled (although the expectations have indeed been set
          # from the most recent actuals).  Treat these as successes
          # instead of failures.
          #
          # TODO(epoger): Do we need to do something similar in
          # other cases, such as when we have recently marked a test
          # as ignoreFailure but it still shows up in the 'failed'
          # category?  Maybe we should not rely on the result_type
          # categories recorded within the gm_actuals AT ALL, and
          # instead evaluate the result_type ourselves based on what
          # we see in expectations vs actual checksum?
          if expected_image_relative_url == actual_image_relative_url:
            updated_result_type = results.KEY__RESULT_TYPE__SUCCEEDED
          else:
            updated_result_type = result_type
          extra_columns_dict = {
              results.KEY__EXTRACOLUMN__RESULT_TYPE: updated_result_type,
              results.KEY__EXTRACOLUMN__BUILDER: builder,
              results.KEY__EXTRACOLUMN__TEST: test,
              results.KEY__EXTRACOLUMN__CONFIG: config,
          }
          try:
            image_pair = imagepair.ImagePair(
                image_diff_db=self._image_diff_db,
                base_url=gm_json.GM_ACTUALS_ROOT_HTTP_URL,
                imageA_relative_url=expected_image_relative_url,
                imageB_relative_url=actual_image_relative_url,
                expectations=expectations_dict,
                extra_columns=extra_columns_dict)
            all_image_pairs.add_image_pair(image_pair)
            if updated_result_type != results.KEY__RESULT_TYPE__SUCCEEDED:
              failing_image_pairs.add_image_pair(image_pair)
          except Exception:
            logging.exception('got exception while creating new ImagePair')

    self._results = {
      results.KEY__HEADER__RESULTS_ALL: all_image_pairs.as_dict(),
      results.KEY__HEADER__RESULTS_FAILURES: failing_image_pairs.as_dict(),
    }


def main():
  logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s',
                      datefmt='%m/%d/%Y %H:%M:%S',
                      level=logging.INFO)
  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--actuals', default=DEFAULT_ACTUALS_DIR,
      help='Directory containing all actual-result JSON files')
  parser.add_argument(
      '--expectations', default=DEFAULT_EXPECTATIONS_DIR,
      help='Directory containing all expected-result JSON files; defaults to '
      '\'%(default)s\' .')
  parser.add_argument(
      '--outfile', required=True,
      help='File to write result summary into, in JSON format.')
  parser.add_argument(
      '--results', default=results.KEY__HEADER__RESULTS_FAILURES,
      help='Which result types to include. Defaults to \'%(default)s\'; '
      'must be one of ' +
      str([results.KEY__HEADER__RESULTS_FAILURES,
           results.KEY__HEADER__RESULTS_ALL]))
  parser.add_argument(
      '--workdir', default=DEFAULT_GENERATED_IMAGES_ROOT,
      help='Directory within which to download images and generate diffs; '
      'defaults to \'%(default)s\' .')
  args = parser.parse_args()
  results = Results(actuals_root=args.actuals,
                    expected_root=args.expectations,
                    generated_images_root=args.workdir)
  gm_json.WriteToFile(
      results.get_packaged_results_of_type(results_type=args.results),
      args.outfile)


if __name__ == '__main__':
  main()
