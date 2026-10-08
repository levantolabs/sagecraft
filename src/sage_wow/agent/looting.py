"""Persistent screenshot-verified loot sweep; input is never completion."""
from sage_wow.agent.cycle import ActionCandidate


class LootSweep:
    def __init__(self, state):
        self.data = state.setdefault('loot_sweep', {
            'pending': bool(state.pop('loot_pending', False)),
            'suspected_kills': [], 'attempts': 0, 'confirmations': 0,
        })

    @property
    def pending(self):
        return self.data['pending']

    @property
    def verification_available(self):
        # The grinding caller validates a completed corpse click's source,
        # session and input generation before setting this optional authority.
        return bool(self.data['attempts'] or self.data.get('click_verification_current'))

    def suspect_kill(self, action_frame):
        if action_frame in self.data['suspected_kills']:
            return
        self.data['suspected_kills'].append(action_frame)
        self.data['suspected_kills'] = self.data['suspected_kills'][-32:]
        self.data.update(pending=True, confirmations=0, attempts=0, search_steps=0, uncertain_frames=0,
                         click_verification_current=False, click_loot_authority=None)

    def interacted(self):
        self.data['attempts'] += 1
        self.data['confirmations'] = 0

    def searched(self):
        self.data['search_steps'] = self.data.get('search_steps', 0) + 1
        self.data['confirmations'] = 0

    def observe(self, choice, frame_id, *, passive_stalled=False):
        if self.data.get('observation_frame') == frame_id:
            return
        self.data['observation_frame'] = frame_id
        self.data['observation'] = choice
        if choice == 'loot_uncertain':
            self.data['uncertain_frames'] = self.data.get('uncertain_frames', 0) + 1
        if choice == 'loot_unavailable' and self.data.get('search_steps', 0) >= self.data.get('search_limit', 8):
            self.data.update(pending=False, confirmations=0, outcome='unavailable_after_search_not_verified_looted')
            return
        if choice == 'loot_unavailable' and passive_stalled:
            self.data.update(pending=False, confirmations=0, outcome='unverified_after_passive_stall')
            return
        if choice == 'loot_verified' and self.verification_available:
            self.data['confirmations'] += 1
            if self.data['confirmations'] >= 2:
                self.data.update(pending=False, attempts=0, confirmations=0, outcome='verified_looted')
        else:
            self.data['confirmations'] = 0

    def candidates(self, bindings=None, *, passive_stalled=False):
        options = {
            'under_attack': 'Visible hostile NPC actively attacking the player or clear ongoing combat. A selected living target alone is NOT evidence of being attacked.',
            'loot_remaining': 'A lootable corpse from OUR recent fight or our loot window remains; approach and loot before leaving. Unrelated corpses from other players are not our loot obligation.',
            'loot_uncertain': 'Cannot establish whether all recent corpses are empty. Missing target, closed loot window, quest count, and lack of visible corpses alone do NOT prove completion.',
        }
        if self.verification_available:
            options['loot_verified'] = 'Positive visual evidence that ALL corpses from the recent fight have been emptied after interaction, with no items remaining in any open loot window and no remaining lootable corpse. Never infer success just from pressing F or gaining one item.'
        searches = []
        bindings = bindings or {}
        # Stay in the same location and rotate consistently through a full sweep.
        turn = bindings.get('turn_left', {})
        if turn.get('verified_from'):
            searches.append(ActionCandidate('loot_search',
                'Corpse location uncertain or off screen: turn left to actively search the surrounding ground. '
                'Choose this instead of passively inspecting the same view. No walking away.',
                {'type': 'keypress', 'keycode': turn['keycode'], 'hold_seconds': self.data.get('search_seconds', .6)}))
        if searches and self.data.get('uncertain_frames', 0) >= 2:
            options.pop('loot_uncertain')
        if self.data.get('search_steps', 0) >= self.data.get('search_limit', 8):
            searches = []
            options.pop('loot_uncertain', None)
            options['loot_unavailable'] = ('A full surrounding search has been performed, and no recoverable lootable corpse from OUR recent fight or our loot window '
                'can be found. End this failed recovery and resume gameplay. This records unavailable, NOT successfully looted. '
                'Choose loot_remaining if our lootable corpse remains reachable. Unrelated corpses and other players kills do not block this recovery.')
        if passive_stalled:
            options.pop('loot_remaining', None)
            options.pop('loot_uncertain', None)
            options['loot_unavailable'] = ('Recovery attempts have not resolved this loot task for three active minutes. '
                'If no supported corpse interaction remains, end this recovery as UNVERIFIED and resume hunting. '
                'This does not claim successful looting, a completed surrounding search, death or kill credit. '
                'Choose a corpse point or active search instead if it can usefully recover our loot.')
        return [ActionCandidate(k, v, {'type': 'observe_only'}) for k, v in options.items()] + searches


def corpse_proposals(path, width, height, *, nearby_ground=False):
    """Locally dark bodies on nearby ground are proposals, never death facts."""
    from PIL import Image, ImageChops, ImageFilter, ImageOps
    with Image.open(path) as source:
        im = source.convert('RGB').resize((width // 2, height // 2))
    pixels = im.load()
    gray = ImageOps.grayscale(im)
    # Absolute darkness marks the entire blue snow field at night. Contrast
    # against local ground instead keeps isolated bodies and remains fallible.
    candidate_pixels = None
    if nearby_ground:
        contrast = ImageChops.subtract(gray.filter(ImageFilter.GaussianBlur(12)), gray)
        mask = contrast.point(lambda value: 255 if value >= 10 else 0)
        mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        candidate_pixels = mask.load()
    seen = set()
    shapes = []
    x0, x1 = (int(im.width * .24), int(im.width * .78)) if nearby_ground else (int(im.width * .04), int(im.width * .81))
    y0, y1 = (int(im.height * .42), int(im.height * .86)) if nearby_ground else (int(im.height * .40), int(im.height * .63))

    def dark(x, y):
        if not (x0 <= x < x1 and y0 <= y < y1):
            return False
        if nearby_ground and im.width * .465 < x < im.width * .535 and im.height * .47 < y < im.height * .62:
            return False  # Player model, never a corpse candidate.
        if not nearby_ground and im.width * .40 < x < im.width * .60 and y > im.height * .47:
            return False
        if nearby_ground and x < im.width * .31 and y < im.height * .50:
            return False  # Own player portrait/bar UI.
        rgb = pixels[x, y]
        return bool(candidate_pixels[x, y]) and max(rgb) < 170 if nearby_ground else max(rgb) < 155 and max(rgb)-min(rgb) < 65

    for y in range(y0, y1):
        for x in range(x0, x1):
            if (x, y) in seen or not dark(x, y):
                continue
            stack = [(x, y)]
            seen.add((x, y))
            component = []
            while stack:
                px, py = stack.pop()
                component.append((px, py))
                for nx, ny in ((px-1, py), (px+1, py), (px, py-1), (px, py+1)):
                    if (nx, ny) not in seen and dark(nx, ny):
                        seen.add((nx, ny))
                        stack.append((nx, ny))
            xs, ys = zip(*component)
            w, h = max(xs)-min(xs)+1, max(ys)-min(ys)+1
            if nearby_ground and 5 <= w <= 100 and 3 <= h <= 95 and .35 <= w / h <= 7 and len(component) >= 25:
                # Closest visible ground receives useful proposals first; this
                # ordering supplies choices without asserting corpse identity.
                cx, cy = (min(xs)+max(xs)), (min(ys)+max(ys))
                score = len(component) / (1 + 4 * abs(cx / width - .5))
                shapes.append((score, cx, cy))
            elif not nearby_ground and 4 <= w <= 40 and 2 <= h <= 15 and w >= h*1.5 and len(component) >= 7:
                shapes.append((len(component), min(xs)+max(xs), min(ys)+max(ys)))
    return [ActionCandidate(f'corpse_select_{i}',
        f'Left-click marked point corpse_select_{i} to SELECT a corpse only if the marker lies on a visibly dead creature body. '
        'These are unverified shape proposals: reject living creatures, players, bushes and rocks. Selection alone does not loot.',
        {'type': 'click', 'image_x': x, 'image_y': y})
        for i, (_, x, y) in enumerate(sorted(shapes, reverse=True)[:8])]
