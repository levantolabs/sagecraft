"""Grind-local error proposals and stable evidence signatures, never authority."""
import hashlib
import json
import re

ERROR_PATTERNS = {
    'range': r'out of range|too far away',
    # The client uses both phrases; focused OCR also recorded "berin"
    # for "be in". Keep this typed so the existing guarded turns are offered.
    'facing': (r'not facing|wrong direction|face the target|target is not in front|'
               r'facing the wrong way|target needs to be(?:\s+in|rin)\s+front of you'),
    'los': r'line of sight|cannot see',
    'no_target': r'no target|select a target|invalid target',
    'dead_target': r'target is dead',
    'mana': r'not enough mana|insufficient mana|no mana',
    'interrupted': r'interrupted',
    'standing': r'must be standing|must stand|while sitting',
    'unavailable_spell': r'spell not learned|unknown spell|not known|do not know|not available',
    'resist': r'resist|immune',
    'evade': r'evade',
    'cooldown': r'not ready yet|cooldown|another action is in progress|already casting',
}


def error_proposals(rows, frame):
    # Chat logs and upper-left unit labels are not current center-screen errors.
    result = []
    for row in rows:
        bounds = row.bounds
        if row.confidence < .75 or not (.18 * frame.width <= bounds['x'] <= .82 * frame.width
                                      and .12 * frame.height <= bounds['y'] <= .65 * frame.height):
            continue
        for kind, pattern in ERROR_PATTERNS.items():
            if re.search(pattern, row.text, re.I):
                result.append({'kind': kind, 'text': row.text.strip()})
                break
    return result


def evidence_signature(target, measurement, ui, level, *, resource=None):
    relevant = {'name': target.get('name'), 'levels': target.get('levels'),
                'self': target.get('self_target'), 'dead': target.get('invalid_text'),
                'position': measurement.get('position'), 'zone': measurement.get('zone_proposals'),
                'errors': measurement.get('error_cues', []), 'ui': ui.get('cues'),
                'level': level, 'resource': resource}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()
