from argparse import Namespace

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.ui_layout import UILayoutError, validate


def layout():
    return {'version': 1, 'id': 'synthetic-moved-ui', 'provenance': 'offline operator fixture',
        'image_width': 1000, 'image_height': 1000, 'regions': {
            'player_frame': [20, 300, 220, 400], 'player_health': [50, 330, 200, 340],
            'coordinate_primary': [600, 500, 750, 520], 'coordinate_alternate': [590, 490, 760, 530],
            'coordinate_magnification': [580, 480, 770, 540], 'minimap': [600, 200, 850, 450],
            'local_caption': [600, 160, 850, 190], 'quest_tracker': [650, 550, 950, 620]}}


def image_frame(tmp_path):
    from sage_wow.models import Frame
    path = tmp_path/'moved.png'
    im = Image.new('RGB', (1000, 1000), '#202020')
    draw = ImageDraw.Draw(im)
    draw.rectangle((20, 300, 219, 399), fill='#912ae3')
    draw.rectangle((50, 330, 199, 339), fill='#00dd00')
    draw.rectangle((580, 480, 769, 539), fill='#e3912a')
    draw.rectangle((600, 200, 849, 449), fill='#2a91e3')
    draw.rectangle((650, 550, 949, 619), fill='#ee31ac')
    # Old HUD locations are empty, so a stale crop cannot pass this check.
    im.save(path)
    return Frame.create('offline', 1000, 1000, image_path=str(path))


@pytest.mark.parametrize('mutation', [
    lambda x: x.update(version=True), lambda x: x.update(image_width=1001.5),
    lambda x: x.update(provenance=''), lambda x: x['regions'].update(player_health=[0, 0, 1001, 20]),
    lambda x: x['regions'].update(player_health=[50, 330, 50, 340]),
    lambda x: x['regions'].update(player_health=[False, 330, 200, 340]),
    lambda x: x['regions'].pop('player_frame'), lambda x: x['regions'].pop('local_caption'),
    lambda x: x['regions'].update(unknown=[1, 2, 3, 4]),
    lambda x: x['regions'].update(player_health=[1, 2, 10, 20]),
])
def test_malformed_operator_layout_is_rejected(mutation):
    value = layout()
    mutation(value)
    with pytest.raises(UILayoutError):
        validate(value)


def test_profile_rejects_invalid_layout_before_runtime(tmp_path):
    import yaml
    from sage_wow.config import load_profile
    value = layout(); value['regions']['player_health'] = [1, 2, 3, 4]
    path = tmp_path/'profile.yaml'
    path.write_text(yaml.safe_dump({'game': {'edition': 'wow_forever'}, 'character': {'class': 'priest'},
                                 'calibration': {'ui_layout': value}}))
    with pytest.raises(UILayoutError):
        load_profile(path)


def test_configure_and_calibrate_preserve_layout_until_explicit_operator_update(tmp_path, monkeypatch):
    import yaml
    from sage_wow import app
    from sage_wow.config import load_profile
    from sage_wow.agent.ui_layout import validate_frame
    from sage_wow.models import Frame
    path = tmp_path/'profile.yaml'
    path.write_text(yaml.safe_dump({'game': {'edition': 'wow_forever'}, 'character': {'class': 'priest'},
        'client': {'window_id': 1}, 'calibration': {'window_id': 1, 'ui_layout': layout()}}))
    monkeypatch.setattr(app, 'window_details', lambda _: {'owner': 'Offline', 'title': 'Fixture',
        'bounds': {'X': 0, 'Y': 0, 'Width': 800, 'Height': 600}})
    args = app.build_parser().parse_args(['--profile', str(path), 'configure', '--window-id', '2'])
    assert app.configure(args) == 0
    assert load_profile(path).calibration['ui_layout'] == layout()
    image_path = tmp_path/'changed-size.png'
    Image.new('RGB', (800, 600)).save(image_path)
    frame = Frame.create('offline', 800, 600, image_path=str(image_path))
    monkeypatch.setattr(app, 'capture_window', lambda *_: frame)
    assert app.calibrate(Namespace(profile=str(path), output_dir=str(tmp_path))) == 0
    reloaded = load_profile(path)
    assert reloaded.calibration['ui_layout'] == layout()
    assert reloaded.calibration['captured_image_size'] == {'width': 800, 'height': 600}
    with pytest.raises(UILayoutError, match='Recalibrate'):
        validate_frame(frame, reloaded)
