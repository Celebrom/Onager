# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Provides a layer of abstraction for the issue tracker API."""

import json
import logging

from apiclient import discovery
from apiclient import errors
import httplib2

_DISCOVERY_URI = ('https://monorail-prod.appspot.com'
                  '/_ah/api/discovery/v1/apis/{api}/{apiVersion}/rest')


class IssueTrackerService(object):
  """Class for updating bug issues."""

  def __init__(self, http=None, additional_credentials=None):
    """Initializes an object for adding and updating bugs on the issue tracker.

    This object can be re-used to make multiple requests without calling
    apliclient.discovery.build multiple times.

    This class makes requests to the Monorail API.
    API explorer: https://goo.gl/xWd0dX

    Args:
      http: A Http object to pass to request.execute; this should be an
          Http object that's already authenticated via OAuth2.
      additional_credentials: A credentials object, e.g. an instance of
          oauth2client.client.SignedJwtAssertionCredentials. This includes
          the email and secret key of a service account.
    """
    self._http = http or httplib2.Http()
    if additional_credentials:
      additional_credentials.authorize(self._http)
    self._service = discovery.build(
        'monorail', 'v1', discoveryServiceUrl=_DISCOVERY_URI,
        http=self._http)

  def AddBugComment(self, bug_id, comment, status=None, cc_list=None,
                    merge_issue=None, labels=None, owner=None):
    """Adds a comment with the bisect results to the given bug.

    Args:
      bug_id: Bug ID of the issue to update.
      comment: Bisect results information.
      status: A string status for bug, e.g. Assigned, Duplicate, WontFix, etc.
      cc_list: List of email addresses of users to add to the CC list.
      merge_issue: ID of the issue to be merged into; specifying this option
          implies that the status should be "Duplicate".
      labels: List of labels for bug.
      owner: Owner of the bug.

    Returns:
      True if successful, False otherwise.
    """
    if not bug_id or bug_id < 0:
      return False

    body = {'content': comment}
    updates = {}
    # Mark issue as duplicate when relevant bug ID is found in the datastore.
    # Avoid marking an issue as duplicate of itself.
    if merge_issue and int(merge_issue) != bug_id:
      status = 'Duplicate'
      updates['mergedInto'] = merge_issue
      logging.info('Bug %s marked as duplicate of %s', bug_id, merge_issue)
    if status:
      updates['status'] = status
    if cc_list:
      updates['cc'] = cc_list
    if labels:
      updates['labels'] = labels
    if owner:
      updates['owner'] = owner
    body['updates'] = updates

    return self._MakeCommentRequest(bug_id, body)

  def List(self, **kwargs):
    """Makes a request to the issue tracker to list bugs."""
    request = self._service.issues().list(projectId='chromium', **kwargs)
    return self._ExecuteRequest(request)

  def _MakeCommentRequest(self, bug_id, body, retry=True):
    """Makes a request to the issue tracker to update a bug.

    Args:
      bug_id: Bug ID of the issue.
      body: Dict of comment parameters.
      retry: True to retry on failure, False otherwise.

    Returns:
      True if successful posted a comment or issue was deleted.  False if
      making a comment failed unexpectedly.
    """
    request = self._service.issues().comments().insert(
        projectId='chromium',
        issueId=bug_id,
        sendEmail=True,
        body=body)
    try:
      if self._ExecuteRequest(request, ignore_error=False):
        return True
    except errors.HttpError as e:
      reason = _GetErrorReason(e)
      # Retry without owner if we cannot set owner to this issue.
      if retry and reason == 'The user does not exist':
        del body['updates']['owner']
        return self._MakeCommentRequest(bug_id, body, retry=False)
      # This error reason is received when issue is deleted.
      elif 'User is not allowed to view this issue' in reason:
        logging.warning('Unable to update bug %s with body %s', bug_id, body)
        return True
    logging.error('Error updating bug %s with body %s', bug_id, body)
    return False

  def NewBug(self, title, description, labels=None, components=None,
             owner=None):
    """Creates a new bug.

    Args:
      title: The short title text of the bug.
      description: The body text for the bug.
      labels: Starting labels for the bug.
      components: Starting components for the bug.
      owner: Starting owner account name.

    Returns:
      The new bug ID if successfully created, or None.
    """
    body = {
        'title': title,
        'summary': title,
        'description': description,
        'labels': labels or [],
        'components': components or [],
        'status': 'Assigned',
    }
    if owner:
      body['owner'] = {'name': owner}
    return self._MakeCreateRequest(body)

  def _MakeCreateRequest(self, body):
    """Makes a request to create a new bug.

    Args:
      body: The request body parameter dictionary.

    Returns:
      A bug ID if successful, or None otherwise.
    """
    request = self._service.issues().insert(
        projectId='chromium',
        sendEmail=True,
        body=body)
    response = self._ExecuteRequest(request)
    if response and 'id' in response:
      return response['id']
    return None

  def GetLastBugCommentsAndTimestamp(self, bug_id):
    """Gets last updated comments and timestamp in the given bug.

    Args:
      bug_id: Bug ID of the issue to update.

    Returns:
      A dictionary with last comment and timestamp, or None on failure.
    """
    if not bug_id or bug_id < 0:
      return None
    response = self._MakeGetCommentsRequest(bug_id)
    if response and all(v in response.keys()
                        for v in ['totalResults', 'items']):
      bug_comments = response.get('items')[response.get('totalResults') - 1]
      if bug_comments.get('content') and bug_comments.get('published'):
        return {
            'comment': bug_comments.get('content'),
            'timestamp': bug_comments.get('published')
        }
    return None

  def _MakeGetCommentsRequest(self, bug_id):
    """Makes a request to the issue tracker to get comments in the bug."""
    # TODO (prasadv): By default the max number of comments retrieved in
    # one request is 100. Since bisect-fyi jobs may have more then 100
    # comments for now we set this maxResults count as 10000.
    # Remove this max count once we find a way to clear old comments
    # on FYI issues.
    request = self._service.issues().comments().list(
        projectId='chromium',
        issueId=bug_id,
        maxResults=10000)
    return self._ExecuteRequest(request)

  def _ExecuteRequest(self, request, ignore_error=True):
    """Makes a request to the issue tracker.

    Args:
      request: The request object, which has a execute method.

    Returns:
      The response if there was one, or else None.
    """
    try:
      response = request.execute(http=self._http)
      return response
    except errors.HttpError as e:
      logging.error(e)
      if ignore_error:
        return None
      raise e


def _GetErrorReason(request_error):
  if request_error.resp.get('content-type', '').startswith('application/json'):
    error_json = json.loads(request_error.content).get('error')
    if error_json:
      return error_json.get('message')
  return None
