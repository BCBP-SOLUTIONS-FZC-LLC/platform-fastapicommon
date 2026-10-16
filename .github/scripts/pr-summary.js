// Summarises CI job results: always in the job's step summary, and as a
// single PR comment (posted or updated) when the token may write one.
// Invoked by ci.yml via actions/github-script (keeps JS out of YAML). Ported
// from platform-gincommon (via platform-eventcommon); only the rows differ.
//
// A pull request from a fork gets a read-only GITHUB_TOKEN, whatever the
// job's `permissions:` say, so the comment call answers 403. That must not
// fail this required check: the summary is still written, and the comment is
// skipped with a notice. The check fails only when `changes` did not succeed.
const needs = JSON.parse(process.env.NEEDS_JSON || '{}');
// ci.yml's `changes` job: false when the PR touches only documentation, in
// which case every build/test job below is skipped on purpose.
const docsOnly = process.env.DOCS_ONLY === 'true';

function icon(r) {
  if (r === 'success') return '✅';
  if (r === 'skipped') return '⏭️';
  if (r === 'cancelled') return '🚫';
  return '❌';
}

const coveragePct = needs['validate-test']?.outputs?.pct;

const COVERAGE_LABEL = 'Coverage ≥ 90%';

const rows = [
  ['Tests (unit · integration) · Python 3.11–3.14 · lowest deps',                   'validate-test'],
  [COVERAGE_LABEL,                                                                   'validate-test'],
  ['Code quality (lock · ruff format · black · ruff · mypy --strict · observability standard · gincommon parity · docs · pip-audit · build)', 'validate-quality'],
  ['Example service image build + Dockerfile lint',                                  'build-image'],
  ['Trivy CVE scan',                                                                 'trivy'],
  ['Example service smoke + e2e tests',                                              'smoke'],
];

const table = rows
  .map(function (row) {
    const label = row[0];
    const key   = row[1];
    const r     = needs[key]?.result || 'skipped';
    const value = (label === COVERAGE_LABEL && coveragePct)
      ? parseFloat(coveragePct).toFixed(1) + '%'
      : '`' + r + '`';
    return '| ' + icon(r) + ' | ' + label + ' | ' + value + ' |';
  })
  .join('\n');

const allPassed = rows.every(function (row) {
  return needs[row[1]]?.result === 'success';
});
const changesOk = needs['changes']?.result === 'success';
const headline = !changesOk
  ? '❌ Change detection failed — the pipeline did not run'
  : docsOnly
    ? '📝 Documentation-only change — build and test jobs skipped (docs.yml checks it)'
    : allPassed
      ? '✅ All checks passed — ready to merge'
      : '❌ Some checks failed';

const marker = '<!-- ci-pr-summary -->';
const body = [
  marker,
  '## ' + headline,
  '',
  '| | Check | Result |',
  '|---|---|---|',
  table,
  '',
  '📦 Example service image cached for `linux/amd64` · merges to `main` are signed with Cosign',
  '🔍 [Security tab](https://github.com/' +
    context.repo.owner +
    '/' +
    context.repo.repo +
    '/security)',
].join('\n');

await core.summary.addRaw(body.replace(marker + '\n', ''), true).write();

// 403: read-only token (fork PR). 404 can mean the same for a private repo.
function notPermitted(err) {
  return err && (err.status === 403 || err.status === 404);
}

const isFork = context.payload.pull_request?.head?.repo?.full_name !== undefined &&
  context.payload.pull_request.head.repo.full_name !== context.payload.repository?.full_name;

try {
  const { data: comments } = await github.rest.issues.listComments({
    ...context.repo,
    issue_number: context.issue.number,
    per_page: 100,
  });
  const existing = comments.find(function (c) {
    return c.body?.startsWith(marker);
  });

  if (existing) {
    await github.rest.issues.updateComment({
      ...context.repo,
      comment_id: existing.id,
      body,
    });
  } else {
    await github.rest.issues.createComment({
      ...context.repo,
      issue_number: context.issue.number,
      body,
    });
  }
} catch (err) {
  if (!notPermitted(err)) throw err;
  core.notice(
    'PR comment not posted (' + err.status + (isFork ? ', fork PR: read-only token' : '') +
    '); the summary is in this job\'s step summary.',
  );
}

// If change detection failed, every gated job was skipped, and a skipped job
// counts as passing for branch protection — so this required check fails.
if (!changesOk) {
  core.setFailed('ci.yml changes job did not succeed (' + (needs['changes']?.result || 'missing') + ')');
}
