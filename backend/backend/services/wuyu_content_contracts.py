"""Actual response schemas, not example records that can be mistaken for data."""
from copy import deepcopy

# DMX/Gemini can reject the expanded grammar for bounded page/evidence arrays
# before generating anything. Array cardinality remains a server-side rule;
# the provider grammar only constrains the shape and types of its JSON output.
GEMINI_SCHEMA_TRANSPORT_POLICY = 'gemini-array-cardinality-server-v1'

SPEAKER_NOTES_LIMIT = 16000
_NOTES_OMITTED = '\n\n（原文证据较长，以下已省略；完整原文见阅读草稿与来源清单。）'


def bound_speaker_notes(text, limit=SPEAKER_NOTES_LIMIT):
    """Fit notes into the native limit without dropping the speaking script.

    Notes are the speaking script followed by verbatim source evidence. Long
    evidence (whole PDF pages, chart transcriptions) used to push a page past
    the limit and fail the entire export. The script is kept; trailing evidence
    is cut at a paragraph boundary and the cut is stated in the notes.
    """
    if not isinstance(text, str):
        return ''
    if len(text) <= limit:
        return text
    budget = limit - len(_NOTES_OMITTED)
    head = text[:budget]
    cut = head.rfind('\n\n')
    if cut >= budget // 2:
        head = head[:cut]
    return head.rstrip() + _NOTES_OMITTED


def gemini_transport_schema(formal_schema):
    """Copy a formal schema into Gemini's bounded-complexity wire grammar.

    Traverse schema positions, never arbitrary dictionaries: a user property
    named ``maxItems`` or metadata containing those words is ordinary data.
    Required keys, enums, object closure, types and string constraints survive.
    Semantic consumers still validate cardinality and source ownership locally.
    This is applied once at the provider boundary for every structured phase.
    """
    def visit(schema):
        if not isinstance(schema, dict):
            return deepcopy(schema)
        result = deepcopy(schema)
        kind = schema.get('type')
        if kind == 'array' or isinstance(kind, list) and 'array' in kind or 'items' in schema or 'prefixItems' in schema:
            result.pop('minItems', None)
            result.pop('maxItems', None)
        for keyword in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'):
            values = schema.get(keyword)
            if isinstance(values, dict):
                result[keyword] = {name: visit(value) for name, value in values.items()}
        for keyword in ('items', 'additionalItems', 'additionalProperties', 'unevaluatedItems',
                        'unevaluatedProperties', 'contains', 'propertyNames', 'not', 'if', 'then', 'else'):
            value = schema.get(keyword)
            if isinstance(value, dict):
                result[keyword] = visit(value)
            elif keyword == 'items' and isinstance(value, list):
                result[keyword] = [visit(item) for item in value]
        for keyword in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            values = schema.get(keyword)
            if isinstance(values, list):
                result[keyword] = [visit(value) for value in values]
        return result
    return visit(formal_schema)

def array(items,**bounds):return {'type':'array','items':items,**bounds}
def obj(properties,required=None):
    return {'type':'object','properties':properties,'required':list(properties)if required is None else required,'additionalProperties':False}
TEXT={'type':'string'}
TITLE={'type':'string','minLength':1,'maxLength':100}
COPY={'type':'string','maxLength':240}
POINT={'type':'string','maxLength':140}
IDS=array(TEXT)
# Evidence references are provenance, not visible slide bullets. Permit a
# bounded set of original-paragraph references alongside reader findings;
# visible points, notes and provider-input budgets remain independently bounded.
MAX_SYNTHESIS_FINDINGS=32

def synthesis_schema(count,forms):
    return obj({'pages':array(obj({'number':{'type':'integer'},'title':TITLE,'finding_ids':array(TEXT,maxItems=MAX_SYNTHESIS_FINDINGS),
        'copy':COPY,'points':array(POINT,maxItems=3),'source_asset_ids':array(TEXT,maxItems=2),
        'layout_rationale':{'type':'string','maxLength':300},'speaking_script':{'type':'string','maxLength':400},
        'visual_form':{'type':'string','enum':sorted(forms)}}),minItems=count,maxItems=count)})

def outline_schema(count):
    spec=obj({'number':{'type':'integer'},'title':TITLE,'kind':{'type':'string','enum':['cover','content']},
        'copy':COPY,'points':array(POINT,maxItems=6),'speaker_notes':{'type':'string','maxLength':16000},'evidence_ids':IDS,
        'data_ids':array(TEXT,maxItems=4),'source_asset_ids':array(TEXT,maxItems=2),
        'role':{'type':'string','enum':['cover','body']},'layout':obj({'structure':TEXT,'intent':TEXT})})
    return obj({'outline':array(obj({'number':{'type':'integer'},'title':TEXT}),minItems=count,maxItems=count),
                'slide_specs':array(spec,minItems=count,maxItems=count)})


def gemini_outline_transport_schema(count):
    """Compatibility helper; all Gemini phases use the same boundary policy."""
    return gemini_transport_schema(outline_schema(count))
