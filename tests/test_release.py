import base64
import io
import zipfile
from pathlib import Path

from ttar.release import create_body


def test_cloud_candidate_does_not_promote_live_or_copy_payload_secrets():
    root = Path(__file__).resolve().parents[1]
    old = {'functionId': 'f', 'runtime': 'python312', 'entrypoint': 'index.cloud_handler',
           'resources': {'memory': '268435456'}, 'serviceAccountId': 'sa',
           'environment': {'ALLOWED_CHAT_ID': '-123'},
           'secrets': [{'id': 'secret-id', 'key': 'TG_TOKEN', 'environmentVariable': 'TG_TOKEN'}],
           'tags': ['live'], 'logOptions': {'disabled': False}}
    body = create_body(root, old, 'candidate-abc')
    assert body['tag'] == ['candidate-abc'] and 'live' not in body['tag']
    assert body['logOptions'] == {'disabled': True}
    assert body['secrets'] == old['secrets']
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body['content']))) as archive:
        assert set(archive.namelist()) == {'index.py', 'ttar/telegram.py', 'ttar/__init__.py', 'requirements.txt'}
