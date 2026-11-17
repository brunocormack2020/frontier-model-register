"""Review-only cross-source identifier matching regressions, including negative cases."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from frontier_register import identity as D
from frontier_register import intelligence as I


def api(dev, name, *, aliases=None, status='acknowledged'):
    key=f'api:{dev}:{name}'
    return key, {'candidate_id':key,'source_type':'developer_api','publisher':dev,
       'model':name, 'details':{'api_model_id':name,'aliases':aliases or []},
       'content_sha256':D.digest([dev,name,aliases]),'review':{'status':status},
       'source_url':'https://example.org/model','retrieved_at':'2026-11-16T00:00:00Z'}


def hf(publisher, name, *, status='acknowledged'):
    repo=f'{publisher}/{name}';key=f'hf:{repo}'
    return key, {'candidate_id':key, 'source_type':'huggingface_repository',
       'publisher':publisher,'model':repo,
       'details':{'repository_id':repo,'revision_sha':'a'*40},
       'content_sha256':D.digest(repo), 'review':{'status':status},
       'source_url':f'https://huggingface.co/{repo}', 'retrieved_at':'2026-11-16T00:00:00Z'}


def record(provider, model_id, *, family='', version='', row_id='r1'):
    return {'record_id':row_id, 'deployment_key':'unassigned',
       'vendor':{'Provider':provider,'Exact Model ID':model_id,
                 'Model Family':family,'Version':version},
       'organization':{'Organization Classification':'Restricted'}}


def inventory(*items, records=None):
    return D.node_inventory({'candidates':dict(items)}, {'records':records or []})


class MultiSourceIdentityTests(unittest.TestCase):
    def test_inconsistent_repository_label_does_not_create_match(self):
        nodes=inventory(api('xAI','grok-2'),hf('xai-org','grok-2'))
        repo_key=next(k for k in nodes if k.startswith('intel:hf:'))
        nodes[repo_key]['label']='xai-org/grok-1'
        self.assertEqual([],D.suggestions(nodes,D.empty_state()))

    def test_xai_api_official_hf_short_grok_version(self):
        nodes=inventory(api('xAI','grok-2'),hf('xai-org','grok-2'))
        hints=D.suggestions(nodes,D.empty_state())
        self.assertEqual(1,len(hints))
        self.assertEqual('exact_reported_identifier',hints[0]['match_rule'])
        self.assertEqual('xAI',hints[0]['developer_context'])
        self.assertEqual('same_model_version',hints[0]['proposed_relation'])
        self.assertIn('checkpoint NOT verified',hints[0]['reason'])

    def test_openai_register_to_curated_hf_same_name(self):
        nodes=inventory(hf('openai','gpt-oss-20b'), records=[record('OpenAI','gpt-oss-20b')])
        hints=D.suggestions(nodes,D.empty_state())
        self.assertEqual(1,len(hints))
        self.assertEqual('same_model_version',hints[0]['proposed_relation'])
        self.assertEqual({'organization_register','huggingface_repository'},
                         {nodes[x]['source_type'] for x in (hints[0]['left_id'],hints[0]['right_id'])})

    def test_google_resource_prefix_is_removed_only_for_google_register(self):
        nodes=inventory(hf('google','gemma-3-27b-it'),
                        records=[record('Google','models/gemma-3-27b-it')])
        self.assertEqual(1,len(D.suggestions(nodes,D.empty_state())))
        bad=inventory(hf('openai','gpt-oss-20b'),
                      records=[record('OpenAI','models/gpt-oss-20b')])
        self.assertEqual([],D.suggestions(bad,D.empty_state()))

    def test_google_register_to_curated_hf(self):
        nodes=inventory(hf('google','gemma-3-27b-it'),records=[record('Google','gemma-3-27b-it')])
        self.assertEqual(1,len(D.suggestions(nodes,D.empty_state())))

    def test_api_to_register_same_provider(self):
        nodes=inventory(api('Mistral','mistral-small-3.1'),records=[record('Mistral','mistral-small-3.1')])
        self.assertEqual(1,len(D.suggestions(nodes,D.empty_state())))

    def test_provider_name_collision_is_blocked(self):
        # Mistral can list third-party developer models; same name does not override provenance boundary.
        nodes=inventory(api('Mistral','deepseek-r1'),hf('deepseek-ai','deepseek-r1'))
        self.assertEqual([],D.suggestions(nodes,D.empty_state()))

    def test_unrecognized_publisher_imposter_blocked(self):
        nodes=inventory(api('xAI','grok-2'),hf('community-xai','grok-2'))
        self.assertEqual([],D.suggestions(nodes,D.empty_state()))

    def test_no_cross_version_or_size_suffix_guessing(self):
        nodes=inventory(api('xAI','grok-2'),hf('xai-org','grok-2-2025'),
            hf('xai-org','grok-1'),hf('xai-org','grok-2-mini'))
        self.assertEqual([],D.suggestions(nodes,D.empty_state()))

    def test_mutable_api_alias_is_weaker(self):
        nodes=inventory(api('xAI','grok-5-stable',aliases=['grok-latest']),hf('xai-org','grok-latest'))
        s=D.suggestions(nodes,D.empty_state())
        self.assertEqual(1,len(s))
        self.assertEqual('mutable_api_alias',s[0]['match_rule'])
        self.assertEqual('hosted_offering_of',s[0]['proposed_relation'])
        self.assertIn('mutable',s[0]['reason'])
        q=D.review_queue(nodes,D.empty_state())
        self.assertEqual('hosted_offering_of',q['items'][0]['relation'])

    def test_family_version_is_weaker_than_exact_id(self):
        nodes=inventory(hf('openai','gpt-oss-20b'),
            records=[record('OpenAI','some-internal-opaque-id',family='gpt-oss',version='20b')])
        hints=D.suggestions(nodes,D.empty_state())
        self.assertEqual(1,len(hints))
        self.assertEqual('register_family_version',hints[0]['match_rule'])
        self.assertEqual('same_family',hints[0]['proposed_relation'])

    def test_three_way_links_are_pairwise_not_transitive(self):
        nodes=inventory(api('Mistral','mistral-small-3.1'),hf('mistralai','Mistral-Small-3.1'),
            records=[record('Mistral','mistral-small-3.1')])
        hints=D.suggestions(nodes,D.empty_state())
        self.assertEqual(3,len(hints))
        self.assertEqual(3,len({x['pair_id'] for x in hints}))
        self.assertTrue(all(s['link_status']=='unreviewed' for s in hints))

    def test_alias_and_exact_same_pair_uses_stronger_match(self):
        nodes=inventory(api('xAI','grok-2',aliases=['grok-2']),hf('xai-org','grok-2'))
        hints=D.suggestions(nodes,D.empty_state())
        self.assertEqual('exact_reported_identifier',hints[0]['match_rule'])

    def test_hf_only_diagnostics_and_explanatory_sheet(self):
        nodes=inventory(hf('mistralai','Mistral-Small-3.1'),hf('xai-org','grok-2'))
        summary=D.summary(D.empty_state(),nodes)
        self.assertEqual(0,summary['suggested_pairs'])
        self.assertIn('Hugging Face-only',summary['no_suggestions_reason'])
        with tempfile.TemporaryDirectory() as tmp:
            from openpyxl import Workbook,load_workbook
            p=Path(tmp)/'test.xlsx';w=Workbook();w.active.title='Model Register';w.active['A1']='baseline';w.save(p)
            D.add_excel_sheets(p,D.empty_state(),nodes)
            wb=load_workbook(p)
            self.assertEqual('No automatic suggestions',wb['Identity Suggestions']['A2'].value)
            self.assertEqual('baseline',wb['Model Register']['A1'].value)
            self.assertIn('Match Rule',[x.value for x in wb['Identity Suggestions'][1]])
            wb.close()

    def test_pending_candidate_cannot_be_confirmed_and_register_not_modified(self):
        k,c=api('xAI','grok-2',status='pending')
        nodes=inventory((k,c),hf('xai-org','grok-2'))
        original=copy.deepcopy(nodes)
        q=D.review_queue(nodes,D.empty_state())
        q['items'][0].update(action='confirm',relation='same_model_version',
                            evidence_url='https://x.ai/',review_note='Enough information provided for metadata review')
        with self.assertRaises(I.IntelligenceError):
            D.apply_review(D.empty_state(),nodes,q,'Security Reviewer')
        self.assertEqual(original,nodes)

    def test_confirmed_link_stale_after_source_change(self):
        nodes=inventory(api('xAI','grok-2'),hf('xai-org','grok-2'))
        q=D.review_queue(nodes,D.empty_state())
        q['items'][0].update(action='confirm', relation='same_model_version',
                            evidence_url='https://x.ai/', review_note='Reviewed evidence at the published URL')
        state=D.apply_review(D.empty_state(),nodes,q,'Reviewer')
        self.assertEqual('confirmed',D.summary(state,nodes)['confirmed_links'] and
                         D.link_status(next(iter(state['links'].values())),nodes))
        changed=copy.deepcopy(nodes)
        target=next(k for k in changed if k.startswith('intel:hf:'))
        changed[target]['evidence_sha256']='b'*64
        self.assertEqual('stale_evidence',D.link_status(next(iter(state['links'].values())),changed))

    def test_deterministic_rule_counts_and_no_duplicate(self):
        nodes=inventory(api('xAI','grok-2'),hf('xai-org','grok-2'),
                         api('Mistral','mistral-small-3.1'),hf('mistralai','Mistral-Small-3.1'))
        hints=D.suggestions(nodes,D.empty_state())
        self.assertEqual(2,len(hints))
        self.assertEqual(2,D.summary(D.empty_state(),nodes)['suggestions_by_rule']['exact_reported_identifier'])
        self.assertEqual([x['pair_id'] for x in hints], sorted([x['pair_id'] for x in hints]))


if __name__=='__main__':
    unittest.main()
