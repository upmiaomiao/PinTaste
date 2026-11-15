"""Literal-wrap recovery contracts using synthetic JSON and source catalogs."""
import copy
import json
import unittest

from services.wuyu_errors import PipelineError
from services.wuyu_json_normalization import (LiteralString, parse_object,
    normalized_fields, identifier, source_text)


class NormalizationTests(unittest.TestCase):
    def test_raw_key_wrap_is_recovered_from_known_schema(self):
        value=parse_object('{"re\nlevance":3,"read_material\n_ids":["m1"]}')
        self.assertEqual(normalized_fields(value,{'relevance','read_material_ids'}),{'relevance':3,'read_material_ids':['m1']})

    def test_escaped_key_newline_is_not_a_schema_alias(self):
        value=parse_object('{"re\\nlevance":3}')
        self.assertEqual(list(normalized_fields(value,{'relevance'})),['re\nlevance'])

    def test_spaces_and_tabs_are_not_deleted_from_keys(self):
        for raw in ['{"re levance":3}','{"re\tlevance":3}','{"re\\tlevance":3}']:
            self.assertNotIn('relevance',normalized_fields(parse_object(raw),{'relevance'}))

    def test_raw_key_collision_is_rejected(self):
        value=parse_object('{"relevance":2,"re\nlevance":3}')
        with self.assertRaises(PipelineError):normalized_fields(value,{'relevance'})

    def test_recovered_key_rejects_other_whitespace_confusables(self):
        for other in ['re levance', 're\tlevance', 're\nlevance', 're\rlevance']:
            raw='{"releva\nnce":3,'+json.dumps(other)+':4}'
            with self.assertRaises(PipelineError):normalized_fields(parse_object(raw),{'relevance'})

    def test_unknown_whitespace_key_without_repair_is_not_renamed(self):
        value=parse_object('{"re levance":3}')
        self.assertEqual(normalized_fields(value,{'relevance'}),value)

    def test_duplicate_original_keys_are_rejected(self):
        with self.assertRaises(PipelineError):parse_object('{"x":1,"x":2}')

    def test_identifier_only_matches_allowed_catalog(self):
        value=parse_object('{"id":"550e\n8400"}')['id']
        self.assertEqual(identifier(value,{'550e8400'}),'550e8400')
        self.assertEqual(identifier(value,{'foreign-id'}),value)

    def test_escaped_identifier_break_is_untouched(self):
        value=parse_object('{"id":"550e\\n8400"}')['id']
        self.assertEqual(identifier(value,{'550e8400'}),'550e\n8400')

    def test_ambiguous_identifier_is_not_guessed(self):
        value=parse_object('{"id":"A\nB"}')['id']
        self.assertEqual(identifier(value,{'AB','A B'}),value)

    def test_source_proves_cjk_digit_join(self):
        value=parse_object('{"quote":"下降\n27%"}')['quote']
        self.assertEqual(source_text(value,['本市指标下降27%。']),'下降27%')

    def test_source_proves_english_word_separator(self):
        value=parse_object('{"quote":"industrial\nsolid waste"}')['quote']
        self.assertEqual(source_text(value,['industrial solid waste is retained']),'industrial solid waste')

    def test_existing_legal_source_break_is_preserved_first(self):
        value=parse_object('{"quote":"A\nB"}')['quote']
        self.assertEqual(source_text(value,['A\nB and AB']),value)

    def test_escaped_source_break_is_not_repaired(self):
        value=parse_object('{"quote":"下降\\n27%"}')['quote']
        self.assertEqual(source_text(value,['下降27%']),value)

    def test_existing_spaces_and_raw_tab_preserved(self):
        for raw in ['{"quote":"下降 27%"}','{"quote":"下降\t27%"}']:
            value=parse_object(raw)['quote'];self.assertEqual(source_text(value,['下降27%']),value)

    def test_source_repair_never_crosses_or_invents_source(self):
        value=parse_object('{"quote":"下降\n27%"}')['quote']
        self.assertEqual(source_text(value,['下降72%']),value)

    def test_two_source_candidates_are_ambiguous(self):
        value=parse_object('{"quote":"A\nB"}')['quote']
        self.assertEqual(source_text(value,['AB and A B']),value)

    def test_repeated_exact_occurrences_are_ambiguous(self):
        value=parse_object('{"quote":"A\nB"}')['quote']
        self.assertEqual(source_text(value,['AB AB']),value)

    def test_unicode_escape_positions_and_crlf(self):
        value=parse_object('{"quote":"\\uD83D\\uDE00下降\r\n27%"}')['quote']
        self.assertEqual(source_text(value,['😀下降27%']),'😀下降27%')
        self.assertEqual(value.literal_breaks,(3,4))

    def test_escape_quote_backslash_and_literal_backslash_n(self):
        expected={'quote':'a"b\\n\\c'}
        self.assertEqual(parse_object(json.dumps(expected)),expected)

    def test_fences_prose_and_trailing_commas_preserve_string_data(self):
        for raw in ['```json\n{"x":"A,}",}\n```','Prose {"x":"A,}",} suffix']:
            self.assertEqual(parse_object(raw),{'x':'A,}'})

    def test_truncated_unknown_controls_and_non_object_rejected(self):
        for raw in ['{"x":"unfinished','{"x":[1,2]','{"x":"raw\x00control"}','[1,2]']:
            with self.assertRaises(PipelineError):parse_object(raw)

    def test_metadata_survives_deepcopy_but_does_not_enter_json(self):
        value=parse_object('{"x":"A\nB"}')
        cloned=copy.deepcopy(value);self.assertEqual(cloned['x'].literal_breaks,(1,))
        self.assertEqual(json.loads(json.dumps(value)),{'x':'A\nB'})
        self.assertNotIn('literal_breaks',json.dumps(value))


if __name__=='__main__':unittest.main()
