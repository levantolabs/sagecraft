from types import SimpleNamespace

from sage_wow.agent.progression import session_ui_candidates, filter_session_observations


def test_session_recovery_only_offers_okay_on_disconnected_server_popup():
    observations=[
        SimpleNamespace(text='You have been disconnected from the server. (WOW51900319)',confidence=.98,
                        bounds={'x':400,'y':300,'width':400,'height':40}),
        SimpleNamespace(text='Okay',confidence=.96,
                        bounds={'x':690,'y':520,'width':110,'height':35}),
        SimpleNamespace(text='Reconnect',confidence=.95,
                        bounds={'x':650,'y':575,'width':190,'height':40}),
    ]
    candidates,evidence=session_ui_candidates(observations,1496,967,world_visible=True)
    by_id={c.option:c for c in candidates}
    assert evidence['disconnected_context']
    assert by_id['session_dismiss_disconnected'].binding=={'type':'click','image_x':745,'image_y':538}
    assert by_id['session_reconnect'].binding['type']=='click'
    assert {'session_wait','inspect_session_notice'} <= set(by_id)
    assert all(c.option not in {'create_account','enter_credentials','quit'} for c in candidates)


def test_session_ocr_filter_keeps_popup_buttons_but_ignores_chat_and_credentials():
    observations=[
        SimpleNamespace(text='You have been disconnected from the server (WOW51900319)',confidence=.98,
                        bounds={'x':460,'y':300,'width':500,'height':35}),
        SimpleNamespace(text='Okay',confidence=.96,bounds={'x':690,'y':520,'width':110,'height':35}),
        SimpleNamespace(text='loading please help',confidence=.99,
                        bounds={'x':20,'y':800,'width':300,'height':30}),
        SimpleNamespace(text='Password: secret',confidence=.99,
                        bounds={'x':500,'y':420,'width':250,'height':30}),
    ]
    filtered=filter_session_observations(observations,1496,967)
    assert [o.text for o in filtered]==[
        'You have been disconnected from the server (WOW51900319)','Okay']
    candidates,_=session_ui_candidates(filtered,1496,967,world_visible=True)
    assert 'session_dismiss_disconnected' in {c.option for c in candidates}
    assert all('secret' not in c.description for c in candidates)


def test_session_filter_preserves_configured_character_with_spacing_variance():
    observations=[
        SimpleNamespace(text='Enter World',confidence=.98,bounds={'x':690,'y':800,'width':190,'height':44}),
        SimpleNamespace(text='Examplehero',confidence=.98,bounds={'x':700,'y':350,'width':180,'height':30}),
        SimpleNamespace(text='some random account text',confidence=.99,bounds={'x':20,'y':300,'width':250,'height':25}),
    ]
    filtered=filter_session_observations(observations,1496,967,'Example Hero')
    candidates,evidence=session_ui_candidates(filtered,1496,967,'Example Hero')
    assert evidence['expected_character_visible']
    assert 'session_enter_world' in {c.option for c in candidates}
    assert all('random account' not in c.description for c in candidates)


def test_world_refresh_notice_offers_sage_okay_but_never_refresh_now():
    observations=[
        SimpleNamespace(text='The world around you will refresh in 3 Minutes.',confidence=.98,
                        bounds={'x':410,'y':180,'width':500,'height':34}),
        SimpleNamespace(text='Make sure you are out of combat and in a safe area, or select refresh now.',confidence=.96,
                        bounds={'x':390,'y':210,'width':620,'height':28}),
        SimpleNamespace(text='Refresh Now',confidence=.97,
                        bounds={'x':650,'y':233,'width':150,'height':38}),
        SimpleNamespace(text='Okay',confidence=.96,
                        bounds={'x':820,'y':233,'width':110,'height':38}),
    ]
    filtered=filter_session_observations(observations,1496,967)
    candidates,evidence=session_ui_candidates(filtered,1496,967,world_visible=True)
    options={c.option for c in candidates}
    assert evidence['world_refresh_context']
    assert 'session_acknowledge_refresh' in options
    assert 'session_wait' in options and 'inspect_session_notice' in options
    assert 'session_refresh_now' not in options
    okay=next(c for c in candidates if c.option=='session_acknowledge_refresh')
    assert okay.binding=={'type':'click','image_x':875,'image_y':252}


def test_lower_left_world_refresh_toast_does_not_start_session_branch():
    observations=[
        SimpleNamespace(text='The world around you will refresh in 4 Minutes',confidence=.96,
                        bounds={'x':35,'y':518,'width':245,'height':39}),
        SimpleNamespace(text='Okay',confidence=.96,
                        bounds={'x':35,'y':560,'width':70,'height':28}),
    ]
    filtered=filter_session_observations(observations,1496,967)
    assert filtered==[]
    candidates,evidence=session_ui_candidates(filtered,1496,967,world_visible=True)
    assert not evidence['world_refresh_context']
    assert not {'session_acknowledge_refresh','session_dismiss_disconnected'} & {c.option for c in candidates}


def test_session_reconnect_enter_world_and_unsafe_login_are_sage_gated():
    reconnect=[SimpleNamespace(text='Reconnect',confidence=.97,
                                bounds={'x':650,'y':500,'width':180,'height':42})]
    candidates,evidence=session_ui_candidates(reconnect,1496,967,world_visible=False)
    assert evidence['reconnect_context']
    assert next(c for c in candidates if c.option=='session_reconnect').binding['type']=='click'

    character_screen=[
        SimpleNamespace(text='Example Hero',confidence=.98,bounds={'x':700,'y':350,'width':180,'height':30}),
        SimpleNamespace(text='Enter World',confidence=.96,bounds={'x':690,'y':800,'width':190,'height':44}),
    ]
    candidates,evidence=session_ui_candidates(character_screen,1496,967,'Example Hero')
    assert evidence['expected_character_visible']
    assert 'session_enter_world' in {c.option for c in candidates}
    assert 'selected' in next(c.description for c in candidates if c.option=='session_enter_world')

    unsafe_login=[
        SimpleNamespace(text='Log In',confidence=.99,bounds={'x':700,'y':500,'width':130,'height':38}),
        SimpleNamespace(text='Password',confidence=.99,bounds={'x':500,'y':430,'width':130,'height':30}),
        SimpleNamespace(text='Create Account',confidence=.99,bounds={'x':690,'y':600,'width':200,'height':35}),
        SimpleNamespace(text='Enter World',confidence=.99,bounds={'x':690,'y':800,'width':190,'height':44}),
    ]
    candidates,evidence=session_ui_candidates(unsafe_login,1496,967)
    assert evidence['unsafe_login_or_loading_text']
    assert not {'session_enter_world','session_reconnect','session_dismiss_disconnected','session_acknowledge_refresh'} & {c.option for c in candidates}
    assert {'session_wait','inspect_session_notice'} <= {c.option for c in candidates}
