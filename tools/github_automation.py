"""GitHub runtime operations used by trusted workflow jobs, never by Adapters."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE_BRANCH = 'automation/api-watch-checkpoints'


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', self.repo):
            raise ValueError('Invalid GitHub repository')
        self.token = os.environ['GH_TOKEN']

    def request(self, method, endpoint, data=None, optional=False):
        url = 'https://api.github.com/repos/' + self.repo + endpoint
        req = Request(url, data=None if data is None else json.dumps(data).encode(), method=method,
                      headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json', 'User-Agent': 'TradeJournalAutomation/1.0'})
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except HTTPError as e:
            if optional and e.code == 404:
                return None
            raise RuntimeError(f'GitHub operation failed with HTTP {e.code}; check Actions permissions and branch rules') from None

    def head(self, branch):
        ref = self.request('GET', '/git/ref/heads/' + quote(branch, safe='/'), optional=True)
        return ref['object']['sha'] if ref else None

    def text(self, path, ref):
        item = self.request('GET', '/contents/' + quote(path, safe='/') + '?' + urlencode({'ref': ref}), optional=True)
        if item is None:
            return None
        if item.get('encoding') != 'base64':
            raise ValueError('Unexpected GitHub content encoding')
        return base64.b64decode(item['content']).decode('utf-8')

    def commit_files(self, branch, files, message, base=None):
        before = self.head(branch)
        parent = before or base
        if not parent:
            raise ValueError('No branch parent')
        commit = self.request('GET', '/git/commits/' + parent)
        tree_items = []
        for path, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            blob = self.request('POST', '/git/blobs', {'encoding': 'base64', 'content': base64.b64encode(data).decode()})
            tree_items.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        tree = self.request('POST', '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': tree_items})
        new = self.request('POST', '/git/commits', {'tree': tree['sha'], 'parents': [parent], 'message': message})
        if before:
            self.request('PATCH', '/git/refs/heads/' + quote(branch, safe='/'), {'sha': new['sha'], 'force': False})
        else:
            self.request('POST', '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': new['sha']})
        return new['sha']


def report_markdown(result):
    parts = ['# 官方 API 兼容性候选', '', '此记录不表示已修复或发布 Adapter。生成代码未签名，必须审核；官方页面变化也不一定需要升级。', '']
    for provider, decision in result.get('decisions', {}).items():
        parts += [f'## {provider}', '', f"状态：{decision['status']}", '', decision['summary'], '']
    for change in result.get('changes', []):
        if change['potential_impact']:
            parts.append(f"- {change['provider']} / {change['document_id']}: {change['url']}（新增 {change['added_line_count']} 行，移除 {change['removed_line_count']} 行）")
    if result.get('errors'):
        parts += ['', '部分官方来源抓取失败，旧检查点保留；本报告不代表这些来源已检查成功。']
    if result.get('warnings'):
        parts += ['', '附加官网来源暂不可读，未计为检查成功；主要官方 SDK 来源继续监测，官网读取会在后续运行重试。']
    parts += ['', '下一步：审核接口含义和候选修改，补充相应回归用例。源码审核合并后，在 Actions 手动运行“审核后签名发布”，选择 main 的准确提交和新版本。网页安装仍由用户点击。']
    return '\n'.join(parts) + '\n'


def publish_watch(api, result, root):
    main = api.head('main')
    if not main:
        raise ValueError('Default main branch is missing')
    url = None
    if result.get('actionable'):
        if result.get('signed') is not False:
            raise ValueError('Watcher cannot publish signed packages')
        config = json.loads((root / 'automation/providers.json').read_text())
        allowed = {p['source'] for p in config['providers'].values() if p.get('source')}
        files = {}
        for patch in result.get('patches', []):
            if patch['path'] not in allowed or patch['path'] in files:
                raise ValueError('Non-allowlisted or duplicate candidate path')
            current = api.text(patch['path'], main)
            if current is None or hashlib.sha256(current.encode()).hexdigest() != patch['original_sha256']:
                raise ValueError('main Adapter changed; refusing stale repair')
            files[patch['path']] = patch['content']
        fingerprint = hashlib.sha256(json.dumps({'changes': result['changes'], 'patches': result.get('patches', [])}, sort_keys=True).encode()).hexdigest()[:20]
        branch = 'api-watch/candidate-' + fingerprint
        files[f'automation/reports/{fingerprint}.md'] = report_markdown(result)
        files[f'automation/reports/{fingerprint}.json'] = json.dumps({k: result.get(k, []) for k in ('checked_at', 'changes', 'decisions', 'errors', 'warnings')}, ensure_ascii=False, indent=2) + '\n'
        # Existing candidate branches may have human edits. Never overwrite them.
        if not api.head(branch):
            api.commit_files(branch, files, 'Review official API compatibility changes', main)
        prs = api.request('GET', '/pulls?' + urlencode({'state': 'all', 'head': api.repo.split('/')[0] + ':' + branch}))
        if prs:
            url = prs[0]['html_url']
        else:
            providers = ', '.join(sorted(result['decisions']))
            pr = api.request('POST', '/pulls', {'head': branch, 'base': 'main', 'draft': True,
                    'title': 'API 兼容性待审核：' + providers,
                    'body': report_markdown(result) + '\n自动候选验证运行：' + os.environ.get('GITHUB_SERVER_URL', 'https://github.com') + '/' + api.repo + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', '')})
            url = pr['html_url']
        print('Compatibility draft: ' + url)
    if result.get('state_changed'):
        api.commit_files(STATE_BRANCH, {'watch-state.json': json.dumps(result['state'], sort_keys=True) + '\n'}, 'Record official document fingerprints', main)
    if result.get('errors'):
        raise RuntimeError('Some official sources failed. Successful checkpoints saved; failed sources will be retried.')
    return url


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['state', 'propose', 'publish-release'])
    p.add_argument('--file', type=Path, default=Path('candidate.json'))
    p.add_argument('--root', type=Path, default=ROOT)
    p.add_argument('--expected-main')
    args = p.parse_args()
    api = GitHub()
    if args.command == 'state':
        raw = api.text('watch-state.json', STATE_BRANCH) if api.head(STATE_BRANCH) else None
        args.file.write_text(raw or '{}\n')
    elif args.command == 'propose':
        publish_watch(api, json.loads(args.file.read_text()), args.root)
    else:
        head = api.head('main')
        if not args.expected_main or head != args.expected_main:
            raise ValueError('main changed after approval; re-run release against the new reviewed commit')
        # Only package/index files recorded by the trusted release preparer are accepted.
        record = json.loads(args.file.read_text())
        if record['base_sha'] != head:
            raise ValueError('Release record base mismatch')
        typ, ver = record['type'], record['version']
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', typ) or not re.fullmatch(r'\d+\.\d+\.\d+', ver):
            raise ValueError('Invalid release identity')
        paths = ['registry.json', f'{typ}/{typ}-{ver}.zip', f'{typ}/{typ}-{ver}.sha256']
        files = {name: (args.root / name).read_bytes() for name in paths}
        # Make the commit parent equal to the approved revision. A concurrent main
        # update makes this fast-forward fail instead of rebasing a stale release.
        commit = api.request('GET', '/git/commits/' + head)
        entries = []
        for name, data in files.items():
            blob = api.request('POST', '/git/blobs', {'encoding': 'base64', 'content': base64.b64encode(data).decode()})
            entries.append({'path': name, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        tree = api.request('POST', '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': entries})
        new = api.request('POST', '/git/commits', {'tree': tree['sha'], 'parents': [head], 'message': f'Publish reviewed {typ} Adapter {ver}'})
        api.request('PATCH', '/git/refs/heads/main', {'sha': new['sha'], 'force': False})
        print(f"Published {typ} {ver} in commit {new['sha']}")


if __name__ == '__main__':
    main()
