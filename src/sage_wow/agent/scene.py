"""Current-frame observations. All game facts come from pixels and local OCR."""
from dataclasses import dataclass, asdict
import re
from statistics import median


@dataclass
class Scene:
    frame_id: str
    position: tuple[float, float] | None = None
    position_evidence: dict | None = None
    navigation_zone: str | None = None
    navigation_zone_evidence: dict | None = None
    target_name: str | None = None
    # None/False never imply death: the target may be absent, obscured, or the
    # health bar may be unreadable. Death requires explicit dead-text evidence.
    target_alive: bool | None = None
    target_dead: bool = False
    health: float | None = None
    health_confidence: float | None = None
    target_health: float | None = None
    target_health_confidence: float | None = None
    progress: tuple[int, int] | None = None
    progress_evidence: dict | None = None
    quest_tracker_text: list[str] | None = None
    quest_ready_hint: bool = False
    error: str | None = None
    modal: dict | None = None
    loot_messages: list[str] | None = None
    signature: list[int] | None = None
    ui_layout_evidence: dict | None = None
    health_evidence: dict | None = None

    def context(self):
        result={k:v for k,v in asdict(self).items() if k != 'signature'}
        result['target_state']=self.target_state
        return result

    @property
    def target_state(self) -> str:
        """Tri-state selected-target observation; OCR/pixels remain fallible."""
        if self.target_dead:
            return 'dead'
        if self.target_alive is True:
            return 'alive'
        return 'unknown'


def parse_position(text):
    # The visible unpadded coordinate format does not establish a value from
    # ambiguous leading-zero OCR (e.g. 69.9 read as 09.9). Withhold, never repair.
    match = re.fullmatch(r'\s*(0|[1-9]\d?)[.,](\d)\s*[,;. ]\s*(0|[1-9]\d?)[.,](\d)\s*', text)
    if not match:
        return None
    a,b,c,d=match.groups()
    return float(a+'.'+b),float(c+'.'+d)


def is_input_error(text):
    return any(s in text.lower() for s in (
        'out of range','too far','line of sight','in front','facing',
        'no target','need to be closer','invalid target','target is dead',
        'you need to target','inventory is full','inventory full',
    ))


def _green_bar_measure(crop):
    """Estimate green bar fill robustly to text covering a few scan rows.

    Horizontal text occlusion removes green pixels from individual rows. The
    median row coverage tolerates those interruptions; inconsistent rows lower
    confidence so callers can preserve uncertainty instead of inferring health.
    """
    if crop.width < 4 or crop.height < 3:
        return None, 0.0
    row_coverage=[]
    for y in range(crop.height):
        pixels=crop.crop((0,y,crop.width,y+1)).get_flattened_data()
        green_pixels=sum(g>r*1.2 and g>b*1.3 and g>90 for r,g,b in pixels)
        row_coverage.append(green_pixels/crop.width)
    fill=median(row_coverage)
    mad=median(abs(value-fill) for value in row_coverage)
    confidence=max(0.0,min(1.0,1.0-mad/.25))
    # A mostly unreadable/occluded bar must not supply a health value.
    if confidence < .55 or sum(value>.02 for value in row_coverage)<(crop.height+1)//2:
        return None,round(confidence,3)
    return round(fill,3),round(confidence,3)


def _green_bar_edge_measure(crop):
    """Alternate calibrated bar evidence from two separated fill boundaries.

    Interior glyph holes do not define the endpoint. Both outer scan bands
    must independently support the same left-anchored edge; the full prefix
    must remain mostly green, with no detached green beyond that boundary.
    """
    width,height=crop.size
    if width<12 or height<8:return None,0.,{'reason':'insufficient separated scan geometry'}
    green=[[g>r*1.2 and g>b*1.3 and g>90
            for r,g,b in crop.crop((0,y,width,y+1)).get_flattened_data()]
           for y in range(height)]
    inset=max(1,min(2,round(width*.01)))
    tolerance=max(1,round(width*.01))
    # Outer fifths leave the central glyph area out of the edge estimate.
    band=max(2,height//5)
    endpoints=[]
    for row in green:
        start=next((x for x,value in enumerate(row) if value),width)
        end=next((x for x in range(start,width) if not row[x]),width)
        endpoints.append(end if start<=inset and end>start+inset else None)
    bands=[endpoints[:band],endpoints[-band:]]
    estimates=[]
    for values in bands:
        valid=[value for value in values if value is not None]
        if len(valid)<.8*band:return None,0.,{'reason':'missing independent fill boundary'}
        estimates.append(median(valid))
    if abs(estimates[0]-estimates[1])>tolerance:
        return None,0.,{'reason':'separated fill boundaries disagree'}
    edge=round(median(estimates))
    if edge<=2 or edge/width<=.03:return None,0.,{'reason':'edge too small for alternate'}
    agreements=[sum(value is not None and abs(value-edge)<=tolerance for value in values)/band
                for values in bands]
    if min(agreements)<.6:return None,0.,{'reason':'insufficient independent boundary agreement'}
    # A later green island is contradictory evidence, not additional fill.
    if any(any(row[min(width,edge+tolerance):]) for row in green):
        return None,0.,{'reason':'green beyond proposed boundary'}
    support=sum(sum(row[:edge]) for row in green)/(height*edge)
    confidence=min(sum(agreements)/2,support)
    evidence={'method':'separated_fill_edges','upper_edge':estimates[0],
        'lower_edge':estimates[1],'band_agreement':agreements,'prefix_support':round(support,3)}
    if confidence<.8:return None,round(confidence,3),{**evidence,'reason':'insufficient coherent fill support'}
    return round(edge/width,3),round(confidence,3),evidence


