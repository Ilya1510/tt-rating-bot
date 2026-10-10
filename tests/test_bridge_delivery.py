import importlib
import json
import sys
from types import SimpleNamespace

import pytest
from bridge import storage


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    for key, value in {'TG_TOKEN': 'test', 'TG_CHAT_ID': '-123', 'VK_TOKEN': 'test',
                       'VK_PEER_ID': '2000000001', 'VK_CONFIRM': 'test',
                       'TG_BOT_USERNAME': 'bridge', 'BRIDGE_DATABASE': str(tmp_path/'bridge.db')}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(sys.modules, 'bridge.http', SimpleNamespace())
    sys.modules.pop('bridge.legacy', None)
    module = importlib.import_module('bridge.legacy')
    yield module
    sys.modules.pop('bridge.legacy', None)


def test_real_handler_both_directions_reply_mapping_and_redelivery(bridge, monkeypatch):
    sent = []
    monkeypatch.setattr(bridge, 'send_vk', lambda text, **kw: sent.append(('vk', text, kw)) or 10)
    monkeypatch.setattr(bridge, 'send_tg', lambda text, **kw: sent.append(('tg', text, kw)) or 20)
    monkeypatch.setattr(bridge, 'get_vk_name', lambda _: 'Person')
    update = {'update_id': 1, 'message': {'message_id': 5, 'chat': {'id': -123},
              'from': {'first_name': 'Ilya'}, 'text': 'hello'}}
    bridge.handler({'body': json.dumps(update)}, None)
    bridge.handler({'body': json.dumps(update)}, None)
    assert len(sent) == 1
    event = {'type': 'message_new', 'object': {'message': {'peer_id': 2000000001,
             'from_id': 55, 'conversation_message_id': 11, 'text': 'answer',
             'reply_message': {'conversation_message_id': 10}}}}
    bridge.handler({'body': json.dumps(event)}, None)
    bridge.handler({'body': json.dumps(event)}, None)
    assert len(sent) == 2
    assert sent[-1] == ('tg', '[VK] Person: answer', {'reply_to': 5})
    assert storage.load_mapping()['vk_to_tg']['11'] == 20


def test_failed_delivery_is_not_marked_processed(bridge, monkeypatch):
    def fail(*a, **kw): raise RuntimeError('temporary')
    monkeypatch.setattr(bridge, 'send_vk', fail)
    update = {'update_id': 7, 'message': {'message_id': 9, 'chat': {'id': -123},
              'from': {'first_name': 'Ilya'}, 'text': 'hello'}}
    with pytest.raises(RuntimeError): bridge.handler({'body': json.dumps(update)}, None)
    assert 7 not in storage.load_mapping()['processed_updates']


def test_vk_photo_and_reaction_keep_existing_behavior(bridge, monkeypatch):
    sent = []
    monkeypatch.setattr(bridge, 'send_tg', lambda text, **kw: sent.append((text, kw)) or 20)
    monkeypatch.setattr(bridge, 'get_vk_name', lambda _: 'Person')
    photo = {'type': 'message_new', 'object': {'message': {'peer_id': 2000000001,
             'from_id': 55, 'conversation_message_id': 11, 'attachments': [
                {'type': 'photo', 'photo': {'sizes': [{'width': 100, 'url': 'small'},
                                                    {'width': 1000, 'url': 'large'}]}}]}}}
    bridge.handler({'body': json.dumps(photo)}, None)
    assert sent[-1][1]['photo_url'] == 'large'
    reaction = {'type': 'message_reaction_event', 'object': {'reacted_id': 55, 'reaction_id': 4, 'cmid': 11}}
    bridge.handler({'body': json.dumps(reaction)}, None)
    assert sent[-1] == ('[VK] Person поставил 👍', {'reply_to': 20})
