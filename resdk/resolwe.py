"""Resolwe"""
from __future__ import absolute_import, division, print_function

import ntpath
import os
import re
import sys
import uuid

import requests
import slumber

from six.moves.urllib.parse import urljoin  # pylint: disable=import-error
from requests.exceptions import ConnectionError  # pylint: disable=ungrouped-imports, redefined-builtin

from .resources import Data, Collection, Sample
from .resources.utils import iterate_fields, iterate_schema


CHUNK_SIZE = 90000000
DEFAULT_EMAIL = 'anonymous@genialis.com'
DEFAULT_PASSWD = 'anonymous'
DEFAULT_URL = 'https://dictyexpress.research.bcm.edu'


class Resolwe(object):
    """Resolwe SDK for Python."""

    def __init__(self, email=DEFAULT_EMAIL, password=DEFAULT_PASSWD, url=DEFAULT_URL):
        """
        Connect to Resolwe on desired url and provide credentials.
        """
        self.url = url
        self.auth = ResAuth(email, password, url)
        self.api = slumber.API(urljoin(url, '/api/'), self.auth, append_slash=False)

        self.data = ResolweQuerry(self, Data)
        self.collection = ResolweQuerry(self, Collection)
        self.sample = ResolweQuerry(self, Sample)

    def processes(self, process_name=None):
        """Return a list of Processor objects.

        :param process_name: Name of the process
        :type process_name: string
        :rtype: list of Process objects

        """
        # TODO: Make a better representation? If this is run from command line
        # it outputs thousands of lines... Not really helpful.
        if process_name:
            return [p for p in self.api.process.get() if process_name in p['name']]
        else:
            return self.api.process.get()

    def print_upload_processes(self):
        """Print all upload process names."""
        for process in self.processes():
            if 'upload' in process['category']:
                sys.stdout.write(process['name'] + '\n')

    def print_process_inputs(self, process_name):
        """Print process input fields and types.

        :param process_name: Process object name
        :type process_name: string

        """
        process = self.processes(process_name=process_name)

        if len(process) == 1:
            process = process[0]
        elif len(process) > 1:
            # Multiple processors with same slug, but different versions:
            versions = ([int(x['version']) for x in process])
            # Get index of the latest processor version:
            top_index = sorted(enumerate(versions), key=lambda x: x[1])[-1][0]
            process = process[top_index]
        else:
            raise ValueError('Invalid process name: {}.'.format(process_name))

        for field_schema, _, _ in iterate_schema({}, process['input_schema'], 'input'):
            name = field_schema['name']
            typ = field_schema['type']
            sys.stdout.write("{} -> {}\n".format(name, typ))

    def run(self, slug=None, input={}, descriptor=None,  # pylint: disable=redefined-builtin
            descriptor_schema=None, collections=[], data_name=''):
        """Run process and return the corresponding Data object.

        1. Upload files referenced in inputs
        2. Create Data object with given inputs
        3. Command is run to process inputs into outputs
        4. Return Data object

        The processing runs asynchronously, so the returned Data object
        does not have an OK status nor outputs yet.

        :param slug: Process slug (unique identifier)
        :type slug: str
        :param input: Input values
        :type input: dict
        :param descriptor: Descriptor values
        :type descriptor: dict
        :param descriptor_schema: Descriptor fields
        :type descriptor_schema: list of dicts
        :param collections: Default collections of Data object
        :type collections: list of ints
        :param data_name: Default name of Data object
        :type data_name: string
        :rtype: HTTP Response object

        """
        if ((descriptor and not descriptor_schema) or
                (not descriptor and descriptor_schema)):
            raise ValueError("Set both or neither descriptor and descriptor_schema")

        process = self.api.process.get(slug=slug, ordering='-version', limit=1)

        if len(process) == 1:
            process = process[0]
        elif len(process) == 0:
            raise ValueError("Could not get process for given slug")
        else:
            raise ValueError("Unexpected behaviour trying to get the process for given slug")

        # Upload files in basic:file fields
        try:
            for schema, fields in iterate_fields(input, process['input_schema']):
                field_name = schema['name']
                field_type = schema['type']
                field_value = fields[field_name]

                if field_type.startswith('basic:file:'):
                    if not os.path.isfile(field_value):
                        raise ValueError("File {} not found.".format(field_value))

                    file_temp = self._upload_file(field_value)

                    if not file_temp:
                        raise Exception("Upload failed for {}.".format(field_value))

                    file_name = ntpath.basename(field_value)
                    fields[field_name] = {
                        'file': file_name,
                        'file_temp': file_temp
                    }

        except KeyError as key_error:
            raise KeyError("Field '{}' not in process '{}' input schema".format(key_error.args[0], process['slug']))

        data = {
            'process': process['slug'],
            'input': input,
        }

        if data_name:
            data['name'] = data_name

        if descriptor and descriptor_schema:
            data['descriptor'] = descriptor
            data['descriptor_schema'] = descriptor_schema

        if len(collections) > 0:
            data['collections'] = collections

        response_fields = self.api.data.post(data)
        return Data(self.api.data(response_fields['id']), self, response_fields)

    def _upload_file(self, fn):
        """Upload a single file on the platform.

        File is uploaded in chunks of 1,024 bytes.

        :param fn: File path
        :type fn: string

        """
        response = None
        chunk_number = 0
        session_id = str(uuid.uuid4())
        file_size = os.path.getsize(fn)
        base_name = os.path.basename(fn)

        with open(fn, 'rb') as file_:
            while True:
                chunk = file_.read(CHUNK_SIZE)
                if not chunk:
                    break

                for i in range(5):
                    if i > 0 and response is not None:
                        sys.stdout.write("Chunk upload failed (error {}): repeating for chunk \
                                         number {}".format(response.status_code, chunk_number))

                    response = requests.post(urljoin(self.url, 'upload/'),
                                             auth=self.auth,

                                             # request are smart and make
                                             # 'CONTENT_TYPE': 'multipart/form-data;''
                                             files={'file': (base_name, chunk)},

                                             # stuff in data will be in response.POST on server
                                             data={
                                                 '_chunkSize': CHUNK_SIZE,
                                                 '_totalSize': file_size,
                                                 '_chunkNumber': chunk_number,
                                                 '_currentChunkSize': len(chunk),
                                             },
                                             headers={
                                                 'Session-Id': session_id
                                             })
                    if response.status_code in [200, 201]:
                        break
                else:
                    # Upload of a chunk failed (5 retries)
                    return None

                progress = 100. * (chunk_number * CHUNK_SIZE + len(chunk)) / file_size
                sys.stdout.write("\n{:.0f} % Uploaded {}".format(progress, fn))
                sys.stdout.flush()
                chunk_number += 1

        sys.stdout.write("\n")
        return response.json()['files'][0]['temp']


class ResAuth(requests.auth.AuthBase):

    """
    Attach HTTP Resolwe Authentication to Request object.
    """

    def __init__(self, email=DEFAULT_EMAIL, password=DEFAULT_PASSWD, url=DEFAULT_URL):
        payload = {'username': email, 'password': password}

        try:
            response = requests.post(urljoin(url, '/rest-auth/login/'), data=payload)
        except ConnectionError:
            raise ValueError('Server not accessible on {}. Wrong url?'.format(url))

        status_code = response.status_code
        if status_code in [400, 403]:
            raise ValueError('Response HTTP status code {}. Invalid credentials?'.format(status_code))

        if not ('sessionid' in response.cookies and 'csrftoken' in response.cookies):
            raise Exception('Missing sessionid or csrftoken. Invalid credentials?')

        self.sessionid = response.cookies['sessionid']
        self.csrftoken = response.cookies['csrftoken']
        # self.subscribe_id = str(uuid.uuid4())

    def __call__(self, request):
        # modify and return the request
        request.headers['Cookie'] = 'csrftoken={}; sessionid={}'.format(self.csrftoken,
                                                                        self.sessionid)
        request.headers['X-CSRFToken'] = self.csrftoken

        # Not needed until we support HTTP Push with the API
        # if r.path_url != '/upload/':
        #     r.headers['X-SubscribeID'] = self.subscribe_id
        return request


class ResolweQuerry(object):
    """
    Enable querries on data, collection and sample endpoints in Resolwe.

    Each Resolwe instance (for example "re") has threee "endpoints": re.data,
    re.collections and re.sample. Each such andpooint is an instance of
    ResolweQuerry class. ResolweQuerry enables querries on
    corresponding objects, for example:

    re.data.get(42) # return Data object with ID 42.
    re.sample.filter(contributor=1) # return all samples made by contributor 1

    Detailed description of methods can be found in method docstrings.
    """

    def __init__(self, resolwe, Resource):
        self.resolwe = resolwe
        self.resource = Resource
        self.endpoint = Resource.endpoint
        self.api = getattr(resolwe.api, Resource.endpoint)

    def get(self, uid):
        """
        Get object with provided ID or slug.

        :param uid: unique identifier - ID or slug
        :type uid: int(ID) or string(slug)

        :rtype: object of type self.resource
        """
        try:
            if re.match('^[0-9]+$', str(uid)):  # iud is ID number:
                return self.resource(self.api(str(uid)), self.resolwe)
            else:  # uid is slug
                object_json = self.api.get(slug=uid)[0]
                return self.resource(self.api(object_json['id']), self.resolwe, fields=object_json)
        except slumber.exceptions.HttpNotFoundError:
            raise ValueError('Id: "{}" does not exist or you dont have access '
                             'permission.'.format(str(uid)))
        except IndexError:
            raise ValueError('Slug: "{}" does not exist or you dont have '
                             'access permission.'.format(uid))

    def filter(self, **kwargs):
        """
        Return a list of Data objects that match kwargs.

        Querries can be made with the following keywords (and operators)
            * Fields (and operators) for **data** endpoint:
                * slug (=)
                * contributor (=)
                * status (=)
                * name (=)
                * created (=)
                * modified (=)
                * input (=)
                * descriptor (=)
                * started (=)
                * finished (=)
                * output (=)
                * process (=)
                * type (=)
                * collection (=)
            * Fields (and operators) for **collecction** and **sample** endpoint:
                * contributor (=)
                * name (=)
                * description (=)
                * created (=)
                * modified (=)
                * slug (=)
                * descriptor (=)
                * data (=)
                * descriptor_schema (=)
                * id (=)

        Example usage:
        # Get a list of data objects with status set to OK.
        re.data.filter(status='OK')
        # Get a liust of sample objects that contain data object 42 and
        # were contributed by contibutor with ID 1
        re.collection.filter(data=42, contributor=1)

        Note: The filtering options might change (improve) with time.
        """
        return [self.resource(self.api(x['id']), self.resolwe, x) for x in self.api.get(**kwargs)]

    def search(self):
        """
        Full text search.
        """
        raise NotImplementedError()
