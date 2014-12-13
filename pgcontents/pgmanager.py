#
# Copyright 2014 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
PostgreSQL implementation of IPython ContentsManager API.
"""

from base64 import (
    b64decode,
    b64encode,
    encodestring,
)
import mimetypes

from IPython.nbformat import (
    from_dict,
    reads,
    writes,
)
from IPython.utils.traitlets import (
    Instance,
    Unicode,
)
from IPython.html.services.contents.manager import ContentsManager

from sqlalchemy import (
    create_engine,
)
from sqlalchemy.engine.base import Engine
from tornado import web

from schema import (
    dir_exists,
    directories,
    ensure_db_user,
    ensure_directory,
    get_notebook,
    listdir,
    notebooks,
    save_notebook,
    users,
)


NBFORMAT_VERSION = 4


def writes_base64(nb, version=NBFORMAT_VERSION):
    """
    Write a notebook as base64.
    """
    return b64encode(writes(nb, version=version))


def reads_base64(nb, as_version=NBFORMAT_VERSION):
    """
    Read a notebook from base64.
    """
    return reads(b64decode(nb), as_version=as_version)


def _decode_text_from_base64(path, bcontent):
    try:
        return (bcontent.decode('utf-8'), 'text')
    except UnicodeError:
        raise web.HTTPError(
            400,
            "%s is not UTF-8 encoded" % path, reason='bad format'
        )


def _decode_unknown_from_base64(path, bcontent):
    try:
        return (bcontent.decode('utf-8'), 'text')
    except UnicodeError:
        pass
    return encodestring(bcontent).decode('ascii'), 'base64'


def from_b64(path, bcontent, format):
    """
    Decode base64 content for a file.

    format:
      If 'text', the contents will be decoded as UTF-8.
      If 'base64', do nothing.
      If not specified, try to decode as UTF-8, and fall back to base64

    Returns a triple of decoded_content, format, and mimetype.
    """
    decoders = {
        'base64': lambda path, content: (content, 'base64'),
        'text': _decode_text_from_base64,
        None: _decode_unknown_from_base64,
    }
    content, real_format = decoders[format](path, bcontent)

    default_mimes = {
        'text': 'text/plain',
        'base64': 'application/octet-stream',
    }
    mimetype = mimetypes.guess_type(path) or default_mimes[real_format]

    return content, real_format, mimetype


class PostgresContentsManager(ContentsManager):
    """
    ContentsManager that persists to a postgres database rather than to the
    local filesystem.
    """
    db_url = Unicode(
        default_value="postgresql://ssanderson@/pgcontents",
        help="Connection string for the database.",
    )

    user_id = Unicode(
        default_value="ssanderson",
        help="Username for the server we're managing."
    )

    engine = Instance(Engine)

    def _engine_default(self):
        return create_engine(self.db_url)

    def __init__(self, *args, **kwargs):
        super(PostgresContentsManager, self).__init__(*args, **kwargs)
        self.ensure_user()

    def ensure_user(self):
        with self.engine.begin() as db:
            ensure_db_user(db, self.user_id)

        with self.engine.begin() as db:
            ensure_directory(db, self.user_id, '')

    def purge(self):
        """
        Clear all matching our user_id.
        """
        with self.engine.begin() as db:
            db.execute(notebooks.delete().where(
                notebooks.c.user_id == self.user_id
            ))
            db.execute(directories.delete().where(
                directories.c.user_id == self.user_id
            ))
            db.execute(users.delete().where(
                users.c.id == self.user_id
            ))

    # Begin ContentsManager API.
    def dir_exists(self, path):
        with self.engine.begin() as db:
            return dir_exists(db, self.user_id, path)

    def is_hidden(self, path):
        return False

    def file_exists(self, path):
        with self.engine.begin() as db:
            return get_notebook(db, self.user_id, path, include_content=False)

    def _base_model(self, path):
        """
        Return model keys shared by all types.
        """
        return {
            "name": path.rsplit('/', 1)[-1],
            "path": path,
            "writable": True,
            "last_modified": None,
            "created": None,
            "content": None,
            "format": None,
            "mimetype": None,
        }

    def get(self, path, content=True, type=None, format=None):

        if type is None:
            type = self.guess_type(path)

        if type == "notebook":
            return self._get_notebook(path, content, format)
        elif type == "directory":
            return self._get_directory(path, content, format)
        elif type == "file":
            return self._get_file(path, content, format)
        else:
            raise ValueError("Unknown type passed: {}".format(type))

    def guess_type(self, path):
        """
        Guess the type of a file.
        """
        if path.endswith('.ipynb'):
            return 'notebook'
        elif self.dir_exists(path):
            return 'directory'
        else:
            return 'file'

    def _get_notebook(self, path, content, format):
        model = self._base_model(path)
        model['type'] = 'notebook'
        with self.engine.begin() as db:
            nb = get_notebook(db, self.user_id, path, content)

        if content:
            content = reads_base64(nb['content'])
            self.mark_trusted_cells(content, path)
            model['content'] = content
            model['format'] = 'json'
            model['last_modified'] = model['created'] = nb['created_at']
            self.validate_notebook_model(model)
        return model

    def _get_directory(self, path, content, format):
        model = self._base_model(path)
        model['type'] = 'directory'
        if content:
            with self.engine.begin() as db:
                model['content'] = listdir(db, path, self.user_id)
                if model['content'] is None:
                    self.do_404(u'directory not found %s' % path)
        elif not self.dir_exists(path):
            self.do_404(u'directory not found %s' % path)

        return model

    def _get_file(self, path, content, format):
        model = self._base_model(path)
        model['type'] = 'file'
        with self.engine.begin() as db:
            # TODO: Rename this to get_file or somesuch.
            nb = get_notebook(db, self.user_id, path, content)
        if content:
            bcontent = nb['content']
            if bcontent is None:
                self.do_404(u'file not found %s' % path)
            model['content'], model['format'], model['mimetype'] = from_b64(
                path,
                bcontent,
                format,
            )
        return model

    def save(self, model, path):
        if 'type' not in model:
            raise web.HTTPError(400, u'No file type provided')
        if 'content' not in model and model['type'] != 'directory':
            raise web.HTTPError(400, u'No file content provided')

        # Almost all of this is duplicated with FileContentsManager :(.
        self.log.debug("Saving %s", path)
        if model['type'] not in ('file', 'directory', 'notebook'):
            self.do_400("Unhandled contents type: %s" % model['type'])
        try:
            with self.engine.begin() as db:
                if model['type'] == 'notebook':
                    validation_message = self._save_notebook(db, model, path)
                elif model['type'] == 'file':
                    validation_message = self._save_file(db, model, path)
                else:
                    validation_message = self._save_directory(db, path)
        except web.HTTPError:
            raise
        except Exception as e:
            self.log.error(u'Error while saving file: %s %s',
                           path, e, exc_info=True)
            self.do_500(
                u'Unexpected error while saving file: %s %s' % (path, e)
            )

        # TODO: Consider not round-tripping to the database again here.
        model = self.get(path, type=model['type'], content=False)
        if validation_message is not None:
            model['message'] = validation_message
        return model

    def _save_notebook(self, db, model, path):
        """
        Save a notebook.

        Returns a validation message.
        """
        nb_contents = from_dict(model['content'])
        self.check_and_sign(nb_contents, path)
        save_notebook(db, self.user_id, path, writes_base64(nb_contents))
        # It's awkward that this writes to the model instead of returning.
        self.validate_notebook_model(model)
        return model.get('message')

    def _save_file(self, db, model, path):
        """
        Save a non-notebook file.
        """
        fmt = model.get('format', None)
        if fmt not in {'text', 'base64'}:
            self.do_400(
                "Must specify format of file contents as 'text' or 'base64'"
            )
        save_notebook(db, self.user_id, path, b64encode(model['content']))
        return None

    def _save_directory(self, db, path):
        """
        'Save' a directory.
        """
        ensure_directory(db, self.user_id, path)

    def update(self, model, path):
        raise NotImplementedError()

    def delete(self, path):
        raise NotImplementedError()

    def create_checkpoint(self, path):
        raise NotImplementedError()

    def list_checkpoints(self, path):
        raise NotImplementedError()

    def restore_checkpoint(self, checkpoint_id, path):
        raise NotImplementedError()
    # End ContentsManager API.

    def do_404(self, msg):
        raise web.HTTPError(404, msg)

    def do_400(self, msg):
        raise web.HTTPError(400, msg)

    def do_500(self, msg):
        raise web.HTTPError(500, msg)
