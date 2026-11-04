import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import api_watch as watch
import github_automation as automation


LINES = ['# Trades', 'execution price quantity', 'signature timestamp', 'fee USDT', 'cursor pagination', 'end']


class WatchTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'openai'})
        env.start()
        self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'automation').mkdir()
        (self.root / 'sources/bybit').mkdir(parents=True)
        self.code = "class Adapter:\n    TYPE = 'bybit'\n"
        (self.root / 'sources/bybit/adapter.py').write_text(self.code)
        self.provider = {'name': 'Bybit', 'source': 'sources/bybit/adapter.py', 'keywords': ['execution', 'fee'],
                         'documents': [{'id': 'doc', 'url': 'https://official.example/docs', 'format': 'markdown', 'critical': False}]}
        (self.root / 'automation/providers.json').write_text(json.dumps({'providers': {'bybit': self.provider}, 'max_ai_requests': 2, 'allowed_document_hosts': ['official.example']}))
        self.doc = self.provider['documents'][0]

    def baseline(self):
        return {'doc': watch.source_change(self.doc, self.provider, LINES, None)[0]}

    def test_first_fetch_is_baseline_not_upgrade(self):
        result = watch.scan(self.root, {}, fetcher=lambda *a: LINES)
        self.assertFalse(result['actionable'])
        self.assertTrue(result['state_changed'])
        self.assertFalse(result['changes'])

    def test_unchanged_does_not_create_pr_or_ai_call(self):
        def forbidden(*a):
            raise AssertionError('Unexpected AI call')
        result = watch.scan(self.root, self.baseline(), fetcher=lambda *a: LINES, repair=forbidden)
        self.assertFalse(result['state_changed'])
        self.assertFalse(result['actionable'])

    def test_irrelevant_change_only_advances_fingerprints(self):
        result = watch.scan(self.root, self.baseline(), fetcher=lambda *a: LINES + ['new deposit help'])
        self.assertFalse(result['actionable'])
        self.assertTrue(result['state_changed'])

    def test_relevant_change_generates_unsigned_candidate(self):
        code = self.code + '\n# candidate\n'
        def repair(*a):
            return {'status': 'unsigned_ai_candidate', 'summary': 'fixture'}, {'path': 'sources/bybit/adapter.py', 'content': code, 'original_sha256': watch.sha(self.code)}
        result = watch.scan(self.root, self.baseline(), fetcher=lambda *a: LINES + ['execution updated'], repair=repair)
        self.assertTrue(result['actionable'])
        self.assertFalse(result['signed'])
        self.assertNotIn('_context', result['changes'][0])
        watch.apply_candidate(self.root, result)
        self.assertEqual((self.root / 'sources/bybit/adapter.py').read_text(), code)

    def test_fetch_failure_retains_prior_checkpoint(self):
        def fail(*a):
            raise OSError('fixture')
        before = self.baseline()
        result = watch.scan(self.root, before, fetcher=fail)
        self.assertEqual(result['state'], before)
        self.assertTrue(result['errors'])
        self.assertFalse(result['actionable'])

    def test_optional_source_failure_is_visible_without_blocking_required_sources(self):
        config=json.loads((self.root / 'automation/providers.json').read_text())
        config['providers']['bybit']['documents'].append(dict(self.doc,id='optional',optional=True))
        (self.root / 'automation/providers.json').write_text(json.dumps(config))
        def fetch(doc,*a):
            if doc.get('optional'):raise OSError('fixture optional site outage')
            return LINES
        result=watch.scan(self.root,{},fetcher=fetch)
        self.assertFalse(result['errors'])
        self.assertEqual(result['warnings'][0]['document'],'optional')
        self.assertIn('doc',result['state'])
        self.assertNotIn('optional',result['state'])

    def test_missing_ai_credentials_does_not_claim_repair(self):
        result, patch = watch.ai_repair('bybit', self.provider, [], self.root, '', '')
        self.assertEqual(result['status'], 'manual_review')
        self.assertIsNone(patch)

    def test_responses_output_is_validated_without_executing_code(self):
        code = self.code + '\n# proposed fixture\n'
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps({'requires_change': True, 'summary': 'fixture', 'adapter_code': code})}]}]}
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return json.dumps(response).encode()
        with patch.object(watch, 'ai_open', return_value=Reply()) as send:
            decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'configured-model')
        self.assertEqual(decision['status'], 'unsigned_ai_candidate')
        self.assertEqual(candidate['original_sha256'], watch.sha(self.code))
        self.assertEqual((self.root / 'sources/bybit/adapter.py').read_text(), self.code)
        payload=json.loads(send.call_args.args[0].data)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['text']['format']['type'], 'json_schema')

    def deepseek_reply(self, content, finish='stop', **message_fields):
        response = {'choices': [{'finish_reason': finish, 'message': dict(content=content, **message_fields)}]}
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return json.dumps(response).encode()
        return Reply()

    def test_deepseek_candidate_uses_official_endpoint_and_json_contract(self):
        code = self.code + '\n# deepseek fixture\n'
        proposal = json.dumps({'requires_change': True, 'summary': 'fixture', 'adapter_code': code})
        with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek'}), \
                patch.object(watch, 'ai_open', return_value=self.deepseek_reply(proposal)) as send:
            decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'fixture-model')
        self.assertEqual(decision['status'], 'unsigned_ai_candidate')
        self.assertEqual(candidate['content'], code)
        self.assertEqual((self.root / 'sources/bybit/adapter.py').read_text(), self.code)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
        payload = json.loads(request.data)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertIn('JSON schema:', payload['messages'][0]['content'])
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['max_tokens'], 24000)
        self.assertNotIn('fixture-key', request.data.decode())
        self.assertNotIn('store', payload)

    def test_deepseek_rejects_empty_truncated_and_invalid_schema(self):
        good = {'requires_change': False, 'summary': 'fixture', 'adapter_code': ''}
        cases = [('', 'stop'), ('   ', 'stop'), (json.dumps(good), 'length'),
                 (json.dumps(dict(good, requires_change='true')), 'stop'),
                 (json.dumps(dict(good, adapter_code=123)), 'stop'),
                 (json.dumps(dict(good, unexpected='value')), 'stop')]
        for content, finish in cases:
            with self.subTest(content=content, finish=finish), \
                    patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek'}), \
                    patch.object(watch, 'ai_open', return_value=self.deepseek_reply(content, finish)):
                decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'fixture-model')
                self.assertEqual(decision['status'], 'manual_review')
                self.assertIsNone(candidate)

    def test_deepseek_no_change_and_tool_calls_are_not_applied(self):
        proposal = json.dumps({'requires_change': False, 'summary': 'no supported change', 'adapter_code': ''})
        for fields, expected in [({}, 'ai_no_supported_change'), ({'tool_calls': [{'id': 'fixture'}]}, 'manual_review')]:
            with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek'}), \
                    patch.object(watch, 'ai_open', return_value=self.deepseek_reply(proposal, **fields)):
                decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'fixture-model')
                self.assertEqual(decision['status'], expected)
                self.assertIsNone(candidate)

    def test_deepseek_still_rejects_identity_and_dependency_changes(self):
        for code in ["class Adapter:\n TYPE='gate'\n", 'import subprocess\n' + self.code]:
            proposal = json.dumps({'requires_change': True, 'summary': 'fixture', 'adapter_code': code})
            with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek'}), \
                    patch.object(watch, 'ai_open', return_value=self.deepseek_reply(proposal)):
                decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'fixture-model')
                self.assertEqual(decision['status'], 'manual_review')
                self.assertIsNone(candidate)

    def test_selected_backend_never_falls_back_to_other_key(self):
        env = {'OPENAI_API_KEY': 'openai-fixture', 'ADAPTER_REPAIR_PROVIDER': 'deepseek',
               'ADAPTER_REPAIR_MODEL': 'fixture-model'}
        self.assertFalse(watch.ai_configuration(env)['key'])
        env['DEEPSEEK_API_KEY'] = 'deepseek-fixture'
        with patch.dict(os.environ, env, clear=True):
            calls = []
            def repair(*args):
                calls.append(args)
                return {'status': 'manual_review', 'summary': 'fixture'}, None
            watch.scan(self.root, self.baseline(), fetcher=lambda *a: LINES + ['fee changed'], repair=repair)
        self.assertEqual(calls[0][-2:], ('deepseek-fixture', 'fixture-model'))

    def test_unsupported_backend_never_sends_credentials(self):
        with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'https://unreviewed.example'}), \
                patch.object(watch, 'ai_open') as send:
            decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'fixture-model')
        send.assert_not_called()
        self.assertEqual(decision['status'], 'manual_review')
        self.assertIsNone(candidate)

    def test_ai_redirects_are_blocked(self):
        with self.assertRaises(ValueError):
            watch.BlockAIRedirect().redirect_request(None, None, 307, '', {}, 'https://unreviewed.example')

    def test_connection_check_reads_models_without_generation(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return b'{"data":[{"id":"fixture-model"}]}'
        with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek', 'DEEPSEEK_API_KEY': 'fixture-key',
                                    'ADAPTER_REPAIR_MODEL': 'fixture-model'}), \
                patch.object(watch, 'ai_open', return_value=Reply()) as send:
            watch.verify_ai_connection()
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/models')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)

    def test_connection_check_missing_key_never_sends(self):
        with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek', 'ADAPTER_REPAIR_MODEL': 'fixture-model'}, clear=True), \
                patch.object(watch, 'ai_open') as send:
            with self.assertRaises(ValueError):
                watch.verify_ai_connection()
        send.assert_not_called()

    def test_connection_check_missing_model_and_upstream_errors_are_redacted(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return b'{"data":[]}'
        for kwargs in [{'return_value': Reply()}, {'side_effect': RuntimeError('fixture-private-error')}]:
            with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek', 'DEEPSEEK_API_KEY': 'fixture-key',
                                        'ADAPTER_REPAIR_MODEL': 'fixture-model'}), \
                    patch.object(watch, 'ai_open', **kwargs), self.assertRaises(ValueError) as error:
                watch.verify_ai_connection()
            self.assertNotIn('fixture-private', str(error.exception))

    def test_upstream_error_does_not_expose_credentials(self):
        with patch.dict(os.environ, {'ADAPTER_REPAIR_PROVIDER': 'deepseek'}), \
                patch.object(watch, 'ai_open', side_effect=RuntimeError('fixture-secret-in-upstream-error')):
            decision, candidate = watch.ai_repair('bybit', self.provider, [], self.root, 'fixture-key', 'fixture-model')
        self.assertNotIn('fixture-secret', json.dumps(decision))
        self.assertEqual(decision['status'], 'manual_review')
        self.assertIsNone(candidate)

    def test_code_cannot_change_identity_or_add_dependencies(self):
        for code in ["class Adapter:\n TYPE='gate'\n", "import subprocess\n" + self.code]:
            with self.assertRaises(ValueError):
                watch.validate_repair(self.code, code, 'bybit')

    def test_unapproved_paths_and_stale_bases_are_rejected(self):
        for path, old in [('../registry.json', watch.sha(self.code)), ('sources/bybit/adapter.py', 'wrong')]:
            with self.assertRaises(ValueError):
                watch.apply_candidate(self.root, {'patches': [{'path': path, 'content': self.code, 'original_sha256': old}]})

    def test_html_navigation_and_scripts_are_not_monitored(self):
        html = '<nav>random token</nav><main><h1>Title</h1>' + ''.join('<p>' + x + '</p>' for x in LINES) + '<script>random()</script></main>'
        lines = watch.normalize(html, 'html')
        self.assertNotIn('random token', lines)
        self.assertNotIn('random()', '\n'.join(lines))

    def test_challenge_page_not_treated_as_api_change(self):
        with self.assertRaises(ValueError):
            watch.normalize('Just a moment\na\nb\nc\nd\ne\nf', 'markdown')

    def test_watcher_never_writes_main(self):
        class API:
            repo = 'owner/repo'
            def __init__(self):
                self.commits = []
            def head(self, branch):
                return 'main-sha' if branch == 'main' else None
            def commit_files(self, branch, files, message, base):
                self.commits.append(branch)
            def request(self, method, endpoint, data=None):
                return [] if method == 'GET' else {'html_url': 'https://github.com/owner/repo/pull/1'}
        api = API()
        result = watch.scan(self.root, self.baseline(), fetcher=lambda *a: LINES + ['fee changed'])
        automation.publish_watch(api, result, self.root)
        self.assertTrue(api.commits)
        self.assertNotIn('main', api.commits)
        self.assertIn(automation.STATE_BRANCH, api.commits)


if __name__ == '__main__':
    unittest.main()
