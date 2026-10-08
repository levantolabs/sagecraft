from types import SimpleNamespace

from PIL import Image


def test_scene_target_state_keeps_unreadable_and_absent_targets_unknown():
    from sage_wow.agent.scene import Scene

    assert Scene('empty').target_state == 'unknown'
    assert Scene('unreadable', target_name='Wolf', target_alive=None).target_state == 'unknown'
    assert Scene('empty-bar', target_name='Wolf', target_alive=False).target_state == 'unknown'
    assert Scene('alive', target_name='Wolf', target_alive=True).target_state == 'alive'
    assert Scene('dead', target_name='Wolf', target_alive=None, target_dead=True).target_state == 'dead'
    assert Scene('unknown').context()['target_state'] == 'unknown'


def _position_frame(tmp_path):
    path=tmp_path/'position.png'
    Image.new('RGB',(1000,1000),'#202020').save(path)
    return SimpleNamespace(image_path=str(path),width=1000,height=1000,frame_id='position-test')


def test_position_parser_withholds_ambiguous_padded_digits_without_repair():
    from sage_wow.agent.scene import parse_position
    assert parse_position('28.7, 09.9') is None
    assert parse_position('08.7, 69.9') is None
    assert parse_position('28.7, 9.9') == (28.7, 9.9)
    assert parse_position('0.7, 0.9') == (0.7, 0.9)


