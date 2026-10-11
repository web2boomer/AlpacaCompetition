import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash, createHmac } from 'node:crypto';
import { createRequire } from 'node:module';

const nodeRequire = createRequire(import.meta.url);

function scripts(path) {
  return [...readFileSync(new URL('../../' + path, import.meta.url), 'utf8').matchAll(/          script: \|\n((?:            .*\n|\n)+)/g)]
    .map(match => match[1].split('\n').map(line => line.slice(12)).join('\n'));
}

const invalidate = scripts('.github/workflows/auto-merge.yml')[0];
const coordinator = scripts('.github/workflows/auto-merge.yml')[1];
const reviewTargetResolver = scripts('.github/workflows/codex-review.yml')[1];
const reviewPublisher = scripts('.github/workflows/codex-review.yml').at(-1);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function reviewRecord(head = 'head', base = 'base', number = 1) {
  const evidence = ['v1', 'owner/repo', String(number), head, base, 'success', 'pass'].join('\n');
  const signature = createHmac('sha256', 'fixture-token').update(evidence).digest('hex');
  return '<!-- codex-review-record head=' + head + ' base=' + base + ' sig=' + signature + ' -->\n' +
    '```json\n' + JSON.stringify({
      state: 'success', reviewed_head_sha: head, reviewed_base_sha: base,
      decision: 'pass', findings: [],
    }, null, 2) + '\n```';
}

function admissionRecord(head = 'head', base = 'base', number = 1, event = 10,
  body = '## Merge dependencies\nNone') {
  const bodyHash = createHash('sha256').update(body).digest('hex');
  const evidence = ['admission-v1', 'owner/repo', String(number), head, base, bodyHash,
    String(event)].join('\n');
  const signature = createHmac('sha256', 'fixture-token').update(evidence).digest('hex');
  return '<!-- automerge-admission head=' + head + ' base=' + base + ' body=' + bodyHash +
    ' event=' + event +
    ' sig=' + signature + ' -->\n' +
    'Owner admission recorded for this exact pull request head and base.';
}

function agentRecord(head = 'head', body = '## Merge dependencies\nNone', number = 1, event = 11) {
  const bodyHash = createHash('sha256').update(body).digest('hex');
  const evidence = ['agent-v1', 'owner/repo', String(number), head, 'main', bodyHash, String(event)].join('\n');
  const signature = createHmac('sha256', 'fixture-token').update(evidence).digest('hex');
  return '<!-- automerge-agent-submission head=' + head + ' base_ref=main body=' + bodyHash +
    ' event=' + event + ' sig=' + signature + ' -->\n' +
    'Agent submitted this exact pull request head and description.';
}

async function reconcile(options = {}) {
  const pr = {
    number: 1, node_id: 'PR1', state: 'open', draft: false, mergeable: true,
    mergeable_state: 'clean', created_at: '2026-10-01T00:00:00Z',
    body: '## Merge dependencies\nNone',
    labels: [{ name: 'automerge:ready' }],
    head: { sha: 'head', repo: { full_name: 'owner/repo' } },
    base: { sha: 'base', ref: 'main', repo: { full_name: 'owner/repo' } },
    ...options.pr,
  };
  pr.merge_commit_sha = options.pr?.merge_commit_sha || `merge:${pr.base.sha}:${pr.head.sha}`;
  const timeline = options.timeline || [{ id: 10, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
    label: { name: 'automerge:ready' }, actor: { login: 'owner' } }];
  const comments = options.comments || [
    { id: 20, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:01:00Z',
      body: admissionRecord('head', 'base', 1, 10, options.admissionBody || pr.body) },
    { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:02:00Z', body: reviewRecord() },
  ];
  if (pr.labels.some(label => label.name === 'automerge:agent') && options.agentRecord !== false) {
    comments.push({ id: 22, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:30Z',
      body: agentRecord(options.agentRecordHead || 'head', options.agentRecordBody ||
        '## Merge dependencies\nNone') });
  }
  const statuses = options.statuses || [
    { context: 'Codex review gate', state: 'success', description: 'PASS base=base', created_at: '2026-10-01T00:02:00Z' },
  ];
  const calls = { removed: [], statuses: [], comments: [], mutations: [], ciRefs: [], agentLabels: [] };
  let queued = Boolean(options.queued);
  let pullGetCount = 0;
  const pulls = {
    list() {}, listReviews() {}, listFiles() {},
    get: async ({ pull_number }) => {
      if (pull_number === pr.number) options.onPullGet?.(pr, ++pullGetCount);
      return { data: pull_number === pr.number ? pr :
        options.extraPR?.number === pull_number ? options.extraPR :
        options.dependencyPRs?.[pull_number] || options.dependencyPR || { state: 'open', merged_at: null } };
    },
  };
  const issues = {
    listEventsForTimeline() {}, listComments() {},
    removeLabel: async args => {
      calls.removed.push(args);
      if (args.issue_number === pr.number) {
        pr.labels = pr.labels.filter(label => label.name !== args.name);
      }
      if (options.removeLabelAlreadyGone) {
        const error = new Error('Label does not exist');
        error.status = 404;
        throw error;
      }
    },
    createComment: async args => {
      const comment = { id: 100 + calls.comments.length, user: { login: 'github-actions[bot]' },
        created_at: '2026-10-01T00:03:00Z', body: args.body };
      calls.comments.push(comment);
      return { data: comment };
    },
  };
  const repos = {
    getBranchProtection: async () => {
      if (options.protectionError || !options.protection) throw new Error('Protection unavailable');
      return { data: options.protection };
    },
    listCommitStatusesForRef: async () => ({ data: statuses.slice(0, 100) }),
    createCommitStatus: async args => { calls.statuses.push(args); return { data: args }; },
  };
  const checks = { listForRef() {} };
  const github = {
    rest: {
      pulls, issues, repos, checks,
      git: {
        getRef: async () => ({ data: { object: { sha: options.mainSha || 'base' } } }),
        getCommit: async ({ commit_sha }) => ({ data: { parents: [
          { sha: options.staleMergeBase || commit_sha.split(':')[1] },
          { sha: commit_sha.split(':')[2] },
        ] } }),
      },
    },
    paginate: async (method, args) => method === pulls.list ? [pr, ...(options.extraPR ? [options.extraPR] : [])] :
      method === pulls.listReviews ? options.reviews || [] :
      method === issues.listEventsForTimeline ? options.timelineByPR?.[args.issue_number] || timeline :
      method === issues.listComments ? [...(options.commentsByPR?.[args.issue_number] || comments), ...calls.comments] :
      method === pulls.listFiles ? options.files || [] :
      method === checks.listForRef ? (calls.ciRefs.push(args.ref), [{ id: 1, name: 'unit',
        conclusion: options.failedCiSha === args.ref ? 'failure' : 'success',
        started_at: '2026-10-01T00:02:00Z',
        pull_requests: [{ number: args.ref === 'head2' ? 2 : 1,
          head: { sha: args.ref }, base: { sha: options.staleCiBase ||
            (args.ref === 'head2' ? options.extraPR?.base.sha : pr.base.sha) } }],
      }]) :
      options.statusesBySha?.[args.ref] || statuses,
    graphql: async query => {
      if (query.includes('reviewThreads')) return { node: { reviewThreads: {
        nodes: options.unresolved ? [{ isResolved: false }] : [], pageInfo: { hasNextPage: false },
      } } };
      if (query.includes('enablePullRequestAutoMerge')) {
        calls.mutations.push('enable');
        queued = true;
        return { enablePullRequestAutoMerge: { pullRequest: { number: pr.number, autoMergeRequest: { enabledAt: 'now' } } } };
      }
      if (query.includes('disablePullRequestAutoMerge')) {
        calls.mutations.push('disable');
        queued = false;
        return { disablePullRequestAutoMerge: { pullRequest: { number: pr.number } } };
      }
      return { node: { autoMergeRequest: queued ? { enabledAt: 'now' } : null } };
    },
  };
  const context = {
    repo: { owner: 'owner', repo: 'repo' }, eventName: 'push', payload: {},
    serverUrl: 'https://fixture.invalid', ...options.context,
  };
  await new AsyncFunction('github', 'context', 'core', 'process', 'fetch', 'require', coordinator)(
    github, context,
    { info() {}, warning() {}, setFailed: assert.fail },
    { env: {
      AUTO_MERGE_ENABLED: options.enabled ? 'true' : 'false',
      BRANCH_PROTECTION_VERIFIED: options.protectionVerified ? 'true' : 'false',
      REQUIRED_CI_CHECKS: options.ciListMissing ? '' : '["unit"]',
      MERGE_TOKEN_CONFIGURED: options.mergeTokenMissing ? 'false' : 'true',
      MERGE_TOKEN: options.mergeTokenMissing ? '' : 'fixture-token',
      GITHUB_API_URL: 'https://fixture.invalid',
    } },
    async (url, request) => {
      assert.equal(request.headers.Authorization, 'Bearer fixture-token');
      if (url.endsWith('/issues/1/labels')) {
        assert.deepEqual(JSON.parse(request.body), { labels: ['automerge:ready'] });
        calls.agentLabels.push('automerge:ready');
        pr.labels.push({ name: 'automerge:ready' });
        options.afterAdmissionPost?.(pr, timeline);
        return { ok: true };
      }
      assert.match(request.body, /enablePullRequestAutoMerge/);
      calls.mutations.push('enable');
      queued = true;
      return { ok: true, json: async () => ({ data: {
        enablePullRequestAutoMerge: { pullRequest: { number: pr.number } },
      } }) };
    },
    nodeRequire,
  );
  return calls;
}

test('default-off coordinator never queues a PR', async () => {
  const result = await reconcile();
  assert.deepEqual(result.mutations, []);
  assert.deepEqual(result.removed, []);
  assert.equal(result.statuses.at(-1).state, 'pending');
});

test('turning auto-merge off cancels an existing native queue entry', async () => {
  const result = await reconcile({ queued: true });
  assert.deepEqual(result.mutations, ['disable']);
});

test('auto-merge needs both explicit enablement and protection verification', async () => {
  const result = await reconcile({ enabled: true, queued: true });
  assert.deepEqual(result.mutations, ['disable']);
});

test('missing independent merge credential leaves the queue disabled', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    mergeTokenMissing: true, queued: true });
  assert.deepEqual(result.mutations, ['disable']);
  const workflow = readFileSync(new URL('../../.github/workflows/auto-merge.yml', import.meta.url), 'utf8');
  assert.match(workflow, /MERGE_TOKEN: \$\{\{ secrets\.AUTOMERGE_TOKEN \}\}/);
  assert.match(workflow, /await enableAutoMergeWithIndependentToken\(/);
});

test('enabled coordinator queues only after owner admission and exact review records pass', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true });
  assert.deepEqual(result.mutations, ['enable']);
  assert.equal(result.statuses.at(-1).state, 'success');
});

test('required coordinator status stays pending until agent submission', async () => {
  const result = await reconcile({ pr: { labels: [] }, timeline: [], comments: [] });
  assert.deepEqual(result.mutations, []);
  assert.deepEqual(result.ciRefs, []);
  assert.equal(result.statuses.at(-1).state, 'pending');
  assert.equal(result.statuses.at(-1).description,
    'Awaiting agent submission, exact review, or CI');
});

test('unrelated completed checks do not scan pull requests', async () => {
  const result = await reconcile({ context: {
    eventName: 'check_run', payload: { check_run: { name: 'Unrelated check' } },
  } });
  assert.deepEqual(result.ciRefs, []);
  assert.deepEqual(result.statuses, []);
});

test('a concurrent already-removed label does not abort reconciliation', async () => {
  const result = await reconcile({ removeLabelAlreadyGone: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    context: { eventName: 'pull_request_target', payload: {
      action: 'edited', changes: { body: { from: 'old' } },
      pull_request: { number: 1, head: { sha: 'head' }, base: { ref: 'main' },
        labels: [{ name: 'automerge:agent' }] },
    } },
  });
  assert.equal(result.removed.some(item => item.name === 'automerge:agent'), true);
  assert.equal(result.statuses.at(-1).state, 'pending');
});

test('owner-authenticated agent submission waits for exact review and CI before admission', async () => {
  const submitted = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    timeline: [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
      label: { name: 'automerge:agent' }, actor: { login: 'owner' } }],
    comments: [{ id: 21, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:00Z', body: reviewRecord() }],
  });
  assert.deepEqual(submitted.agentLabels, ['automerge:ready']);
  assert.deepEqual(submitted.mutations, []);
  assert.equal(submitted.statuses.at(-1).state, 'pending');
  const failedCi = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    timeline: [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
      label: { name: 'automerge:agent' }, actor: { login: 'owner' } }],
    comments: [{ id: 21, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:00Z', body: reviewRecord() }],
    failedCiSha: 'head',
  });
  assert.deepEqual(failedCi.agentLabels, []);
});

test('another actor cannot submit a PR as the owner agent', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    timeline: [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
      label: { name: 'automerge:agent' }, actor: { login: 'collaborator' } }],
    comments: [{ id: 21, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:00Z', body: reviewRecord() }],
  });
  assert.deepEqual(result.agentLabels, []);
  assert.deepEqual(result.mutations, []);
});

test('old agent submission cannot admit a changed head or description before invalidation event', async () => {
  const timeline = [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
    label: { name: 'automerge:agent' }, actor: { login: 'owner' } }];
  const review = head => ({ id: 21, user: { login: 'github-actions[bot]' },
    created_at: '2026-10-01T00:02:00Z', body: reviewRecord(head) });
  const changedHead = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }], head: {
      sha: 'head2', repo: { full_name: 'owner/repo' } } },
    timeline, comments: [review('head2')],
    statuses: [{ context: 'Codex review gate', state: 'success', description: 'PASS base=base' }],
  });
  assert.deepEqual(changedHead.agentLabels, []);
  const changedBody = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }], body: '## Merge dependencies\nNone\nChanged scope' },
    timeline, comments: [review('head')],
  });
  assert.deepEqual(changedBody.agentLabels, []);
});

test('description edit between reconciliation and live submission prevents ready label', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    timeline: [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
      label: { name: 'automerge:agent' }, actor: { login: 'owner' } }],
    comments: [{ id: 21, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:00Z', body: reviewRecord() }],
    onPullGet: (pr, count) => {
      if (count === 2) pr.body += '\nChanged scope';
    },
  });
  assert.deepEqual(result.agentLabels, []);
});

test('description edit after ready admission blocks merge before edited event arrives', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    pr: { body: '## Merge dependencies\nNone\nChanged scope' },
    admissionBody: '## Merge dependencies\nNone',
  });
  assert.deepEqual(result.mutations, ['disable']);
  assert.notEqual(result.statuses.at(-1).state, 'success');
});

test('owner agent label event signs only the exact live head and description', async () => {
  const event = { eventName: 'pull_request_target', payload: {
    action: 'labeled', label: { name: 'automerge:agent' }, sender: { login: 'owner' },
    pull_request: { number: 1, state: 'open', draft: false,
      head: { sha: 'head' }, base: { sha: 'base', ref: 'main' },
      body: '## Merge dependencies\nNone', labels: [{ name: 'automerge:agent' }] },
  } };
  const timeline = [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
    label: { name: 'automerge:agent' }, actor: { login: 'owner' } }];
  const signed = await reconcile({ pr: { labels: [{ name: 'automerge:agent' }] },
    context: event, timeline, agentRecord: false });
  assert.equal(signed.comments.some(item => item.body.includes('automerge-agent-submission')), true);
  const stale = await reconcile({ pr: { labels: [{ name: 'automerge:agent' }],
    body: '## Merge dependencies\nNone\nChanged scope' },
    context: event, timeline, agentRecord: false });
  assert.equal(stale.comments.some(item => item.body.includes('automerge-agent-submission')), false);
});

test('a changed PR revision withdraws agent submission before admission', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    context: { eventName: 'pull_request_target', payload: {
      action: 'synchronize', pull_request: { number: 1,
        head: { sha: 'head' }, base: { sha: 'base' },
        labels: [{ name: 'automerge:agent' }] },
    } },
    timeline: [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
      label: { name: 'automerge:agent' }, actor: { login: 'owner' } }],
    comments: [{ id: 21, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:00Z', body: reviewRecord() }],
  });
  assert.equal(result.removed.some(item => item.name === 'automerge:agent'), true);
  assert.deepEqual(result.agentLabels, []);
});

test('withdrawing agent submission revokes signed admission and a queued merge', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    pr: { labels: [{ name: 'automerge:ready' }] },
    context: { eventName: 'pull_request_target', payload: {
      action: 'unlabeled', label: { name: 'automerge:agent' },
      pull_request: { number: 1, head: { sha: 'head' }, base: { sha: 'base' }, labels: [] },
    } },
  });
  assert.equal(result.removed.some(item => item.name === 'automerge:ready'), true);
  assert.equal(result.mutations.includes('disable'), true);
});

test('withdrawal between live check and admission POST revokes the new ready label', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    timeline: [{ id: 11, event: 'labeled', created_at: '2026-10-01T00:00:00Z',
      label: { name: 'automerge:agent' }, actor: { login: 'owner' } }],
    comments: [{ id: 21, user: { login: 'github-actions[bot]' },
      created_at: '2026-10-01T00:02:00Z', body: reviewRecord() }],
    afterAdmissionPost: (pr, timeline) => {
      pr.labels = pr.labels.filter(label => label.name !== 'automerge:agent');
      timeline.push({ id: 12, event: 'unlabeled', created_at: '2026-10-01T00:03:00Z',
        label: { name: 'automerge:agent' }, actor: { login: 'owner' } });
    },
  });
  assert.deepEqual(result.agentLabels, ['automerge:ready']);
  assert.equal(result.removed.some(item => item.name === 'automerge:ready'), true);
  assert.deepEqual(result.mutations, []);
});

test('withdrawing ready or requesting changes withdraws agent submission', async () => {
  const withdrawn = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }] },
    context: { eventName: 'pull_request_target', payload: {
      action: 'unlabeled', label: { name: 'automerge:ready' },
      pull_request: { number: 1, head: { sha: 'head' }, base: { sha: 'base' },
        labels: [{ name: 'automerge:agent' }] },
    } },
  });
  assert.equal(withdrawn.removed.some(item => item.name === 'automerge:agent'), true);
  const requested = await reconcile({ enabled: true, protectionVerified: true,
    pr: { labels: [{ name: 'automerge:agent' }, { name: 'automerge:ready' }] },
    reviews: [{ user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED',
      submitted_at: '2026-10-01T00:02:00Z' }],
  });
  assert.equal(requested.removed.some(item => item.name === 'automerge:ready'), true);
  assert.equal(requested.removed.some(item => item.name === 'automerge:agent'), true);
});

test('wrong base branch, stale main base, or fork PR cannot enter the queue', async () => {
  const wrongBranch = await reconcile({ enabled: true, protectionVerified: true, pr: {
    base: { sha: 'base', ref: 'release', repo: { full_name: 'owner/repo' } },
  } });
  assert.deepEqual(wrongBranch.mutations, []);
  const staleBase = await reconcile({ enabled: true, protectionVerified: true, mainSha: 'new-main' });
  assert.deepEqual(staleBase.mutations, []);
  const fork = await reconcile({ enabled: true, protectionVerified: true, pr: {
    head: { sha: 'head', repo: { full_name: 'contributor/repo' } },
  } });
  assert.deepEqual(fork.mutations, []);
});

test('merge dependency declaration is required and every declared prerequisite must be merged', async () => {
  const missing = await reconcile({ enabled: true, protectionVerified: true, pr: { body: 'No dependency declaration' } });
  assert.deepEqual(missing.mutations, []);
  const unresolved = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\n- #2',
  } });
  assert.deepEqual(unresolved.mutations, []);
  const merged = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\n- #2',
  }, dependencyPR: { state: 'closed', merged_at: '2026-10-02T00:00:00Z' } });
  assert.deepEqual(merged.mutations, ['enable']);
  const secondUnmerged = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\n- #2\n- #3\n\n## Summary\nFeature',
  }, dependencyPRs: { 2: { merged_at: '2026-10-02T00:00:00Z' }, 3: { merged_at: null } } });
  assert.deepEqual(secondUnmerged.mutations, []);
  const bothMerged = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\n- #2\n- #3\n\n## Summary\nFeature',
  }, dependencyPRs: { 2: { merged_at: '2026-10-02T00:00:00Z' }, 3: { merged_at: '2026-10-02T00:01:00Z' } } });
  assert.deepEqual(bothMerged.mutations, ['enable']);
  const malformedSecond = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\n- #2\nmaybe #3\n\n## Summary\nFeature',
  }, dependencyPRs: { 2: { merged_at: '2026-10-02T00:00:00Z' } } });
  assert.deepEqual(malformedSecond.mutations, []);
  const crossRepository = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\n- owner/other-repo#2',
  }, dependencyPR: { state: 'closed', merged_at: '2026-10-02T00:00:00Z' } });
  assert.deepEqual(crossRepository.mutations, []);
  const malformed = await reconcile({ enabled: true, protectionVerified: true, pr: {
    body: '## Merge dependencies\nmaybe #2 is needed',
  } });
  assert.deepEqual(malformed.mutations, []);
});

test('queued-merge invalidation runs before reconciliation in the same workflow', async () => {
  const workflow = readFileSync(new URL('../../.github/workflows/auto-merge.yml', import.meta.url), 'utf8');
  assert.match(workflow, /reconcile:\n    name:[^\n]*\n    needs: invalidate/);
  assert.doesNotMatch(workflow, /  pull_request_review:/);
  assert.match(workflow, /- Review event relay/);
  assert.match(workflow, /github\.event\.workflow_run\.name == 'Review event relay'/);
  const relay = readFileSync(new URL('../../.github/workflows/review-event-relay.yml', import.meta.url), 'utf8');
  assert.match(relay, /  pull_request_review:/);
  assert.match(relay, /  pull_request_review_comment:/);
  assert.match(relay, /github\.event\.pull_request\.base\.ref == 'main'/);
  assert.match(relay, /github\.event\.pull_request\.head\.repo\.full_name == github\.repository/);
  assert.match(relay, /permissions: \{\}/);
  assert.doesNotMatch(relay, /secrets\.|contents: write|statuses: write/);
  const calls = [];
  await new AsyncFunction('github', 'context', invalidate)({
    graphql: async query => {
      if (query.includes('disablePullRequestAutoMerge')) calls.push('disable');
      return { node: { autoMergeRequest: { enabledAt: 'now' } } };
    },
    rest: { repos: { createCommitStatus: async status => calls.push(status.state) } },
  }, {
    repo: { owner: 'owner', repo: 'repo' },
    serverUrl: 'https://fixture.invalid',
    payload: { pull_request: { number: 1, node_id: 'PR1', state: 'open',
      head: { sha: 'head', repo: { full_name: 'owner/repo' } }, base: { ref: 'main' } } },
  });
  assert.deepEqual(calls, ['pending', 'disable']);
});

test('review relay invalidates queued merges before trusted reconciliation', async () => {
  const calls = [];
  const pr = { number: 1, node_id: 'PR1', state: 'open',
    head: { sha: 'head', repo: { full_name: 'owner/repo' } }, base: { ref: 'main' } };
  const github = {
    paginate: async method => method === github.rest.actions.listJobsForWorkflowRun
      ? [{ name: 'Notify trusted coordinator', conclusion: 'success' }] : [pr],
    rest: { actions: { listJobsForWorkflowRun() {}, getWorkflow: async () => ({ data: { id: 12 } }) },
      pulls: { list() {}, get: async () => ({ data: pr }) },
      repos: { createCommitStatus: async status => calls.push(status.state) } },
    graphql: async query => {
      if (query.includes('disablePullRequestAutoMerge')) calls.push('disable');
      return { node: { autoMergeRequest: { enabledAt: 'now' } } };
    },
  };
  await new AsyncFunction('github', 'context', 'core', invalidate)(github, {
    repo: { owner: 'owner', repo: 'repo' }, serverUrl: 'https://fixture.invalid',
    eventName: 'workflow_run', payload: { workflow_run: { id: 42, workflow_id: 12,
      name: 'Review event relay', pull_requests: [{ number: 1 }] } },
  }, { info() {} });
  assert.deepEqual(calls, ['pending', 'disable']);
});

test('skipped review relay cannot churn the trusted merge queue', async () => {
  const calls = [];
  const github = {
    paginate: async () => [{ name: 'Notify trusted coordinator', conclusion: 'skipped' }],
    rest: { actions: { listJobsForWorkflowRun() {}, getWorkflow: async () => ({ data: { id: 12 } }) },
      pulls: { list() {} },
      repos: { createCommitStatus: async () => calls.push('status') } },
    graphql: async () => { calls.push('graphql'); return {}; },
  };
  await new AsyncFunction('github', 'context', 'core', invalidate)(github, {
    repo: { owner: 'owner', repo: 'repo' }, serverUrl: 'https://fixture.invalid',
    eventName: 'workflow_run', payload: { workflow_run: { id: 42, workflow_id: 12,
      name: 'Review event relay', pull_requests: [{ number: 1 }] } },
  }, { info() {} });
  assert.deepEqual(calls, []);
});

test('shared head commits fail closed even when one PR is otherwise admitted', async () => {
  const extraPR = { number: 2, node_id: 'PR2', state: 'open', draft: false,
    mergeable: true, mergeable_state: 'clean', created_at: '2026-10-02T00:00:00Z',
    merge_commit_sha: 'merge:base:head2',
    body: '## Merge dependencies\nNone', labels: [{ name: 'production-gate' }],
    head: { sha: 'head', repo: { full_name: 'owner/repo' } },
    base: { sha: 'base', ref: 'main', repo: { full_name: 'owner/repo' } } };
  const result = await reconcile({ enabled: true, protectionVerified: true, extraPR });
  assert.deepEqual(result.mutations, []);
  assert.equal(result.statuses.some(status => status.state === 'success'), false);
  assert.equal(result.statuses.at(-1).state, 'failure');
});

test('missing or stale owner admission and missing or stale review status fail closed', async () => {
  const noAdmission = await reconcile({ enabled: true, protectionVerified: true, comments: [
    { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:02:00Z', body: reviewRecord() },
  ] });
  assert.deepEqual(noAdmission.mutations, []);
  const staleCodexStatus = await reconcile({ enabled: true, protectionVerified: true, statuses: [
    { context: 'Codex review gate', state: 'success', description: 'PASS base=old-base', created_at: '2026-10-01T00:02:00Z' },
  ] });
  assert.deepEqual(staleCodexStatus.mutations, []);
  const failedReview = await reconcile({ enabled: true, protectionVerified: true, comments: [
    { id: 20, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:01:00Z', body: admissionRecord() },
    { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:02:00Z', body: reviewRecord().replace('"decision": "pass"', '"decision": "changes_requested"') },
  ] });
  assert.deepEqual(failedReview.mutations, []);
});

test('reconciliation before signed owner admission is recorded preserves the owner label but cannot queue', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true, comments: [
    { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:02:00Z', body: reviewRecord() },
  ] });
  assert.deepEqual(result.removed, []);
  assert.deepEqual(result.mutations, []);
  assert.equal(result.statuses.some(status => status.state === 'success'), false);
});

test('a bot-authored review marker without the protected signature cannot admit a PR', async () => {
  const forged = reviewRecord().replace(/sig=[0-9a-f]{64}/, 'sig=' + '0'.repeat(64));
  const result = await reconcile({ enabled: true, protectionVerified: true, comments: [
    { id: 20, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:01:00Z', body: admissionRecord() },
    { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:02:00Z', body: forged },
  ] });
  assert.deepEqual(result.mutations, []);
  assert.equal(result.statuses.some(status => status.state === 'success'), false);
});

test('a bot-authored owner admission without the protected signature cannot admit a changed head', async () => {
  const forged = '<!-- automerge-admission head=head2 base=base event=10 -->';
  const result = await reconcile({ enabled: true, protectionVerified: true,
    pr: { head: { sha: 'head2', repo: { full_name: 'owner/repo' } } },
    comments: [
      { id: 20, user: { login: 'github-actions[bot]' }, created_at: '2026-10-02T00:01:00Z', body: forged },
      { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-02T00:02:00Z', body: reviewRecord('head2') },
    ],
    statuses: [{ context: 'Codex review gate', state: 'success', description: 'PASS base=base',
      created_at: '2026-10-02T00:02:00Z' }],
  });
  assert.deepEqual(result.mutations, []);
  assert.equal(result.statuses.some(status => status.state === 'success'), false);
});

test('review status remains visible after more than one page of commit statuses', async () => {
  const filler = Array.from({ length: 120 }, (_, index) => ({
    context: 'historical-' + index, state: 'success', created_at: '2026-10-01T00:03:00Z',
  }));
  const result = await reconcile({ enabled: true, protectionVerified: true,
    statuses: [...filler, { context: 'Codex review gate', state: 'success',
      description: 'PASS base=base', created_at: '2026-10-01T00:02:00Z' }],
  });
  assert.deepEqual(result.mutations, ['enable']);
});

test('an older failed-CI PR does not block a newer ready PR', async () => {
  const extraPR = { number: 2, node_id: 'PR2', state: 'open', draft: false,
    mergeable: true, mergeable_state: 'clean', created_at: '2026-10-02T00:00:00Z',
    merge_commit_sha: 'merge:base:head2',
    body: '## Merge dependencies\nNone', labels: [{ name: 'automerge:ready' }],
    head: { sha: 'head2', repo: { full_name: 'owner/repo' } },
    base: { sha: 'base', ref: 'main', repo: { full_name: 'owner/repo' } } };
  const result = await reconcile({ enabled: true, protectionVerified: true,
    failedCiSha: 'head', extraPR,
    timelineByPR: { 2: [{ id: 30, event: 'labeled', created_at: '2026-10-02T00:00:00Z',
      label: { name: 'automerge:ready' }, actor: { login: 'owner' } }] },
    commentsByPR: { 2: [
      { id: 31, user: { login: 'github-actions[bot]' }, created_at: '2026-10-02T00:01:00Z', body: admissionRecord('head2', 'base', 2, 30) },
      { id: 32, user: { login: 'github-actions[bot]' }, created_at: '2026-10-02T00:02:00Z', body: reviewRecord('head2', 'base', 2) },
    ] },
    statusesBySha: { head2: [{ context: 'Codex review gate', state: 'success',
      description: 'PASS base=base', created_at: '2026-10-02T00:02:00Z' }] },
  });
  assert.deepEqual(result.mutations, ['enable']);
  assert.equal(result.statuses.some(status => status.sha === 'head2' && status.state === 'success'), true);
  assert.equal(result.statuses.some(status => status.sha === 'head' && status.state === 'success'), false);
  assert.equal(result.ciRefs.includes('head2'), true);
  assert.equal(result.ciRefs.includes('merge:base:head2'), false);
});

test('CI on a stale synthetic merge revision cannot admit the current PR', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    staleMergeBase: 'old-base' });
  assert.deepEqual(result.mutations, []);
  assert.deepEqual(result.ciRefs, []);
});

test('a head check associated with an older base cannot admit the current PR', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true,
    staleCiBase: 'old-base' });
  assert.deepEqual(result.mutations, []);
  assert.deepEqual(result.ciRefs, ['head']);
});

test('changed head invalidates admission; main advance waits for exact-base review', async () => {
  const changedHead = await reconcile({ pr: { head: { sha: 'new-head', repo: { full_name: 'owner/repo' } } } });
  assert.equal(changedHead.removed.length, 1);
  const changedBase = await reconcile({ pr: { base: { sha: 'new-base', ref: 'main', repo: { full_name: 'owner/repo' } } } });
  assert.equal(changedBase.removed.length, 0);
  assert.equal(changedBase.mutations.length, 0);
  const refreshed = await reconcile({ enabled: true, protectionVerified: true, mainSha: 'new-base',
    pr: { base: { sha: 'new-base', ref: 'main', repo: { full_name: 'owner/repo' } } },
    comments: [
      { id: 20, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:01:00Z', body: admissionRecord() },
      { id: 21, user: { login: 'github-actions[bot]' }, created_at: '2026-10-01T00:02:00Z', body: reviewRecord('head', 'new-base') },
    ],
    statuses: [{ context: 'Codex review gate', state: 'success', description: 'PASS base=new-base', created_at: '2026-10-01T00:02:00Z' }],
  });
  assert.deepEqual(refreshed.mutations, ['enable']);
});

test('editing the dependency declaration invalidates owner admission', async () => {
  const pr = { number: 1, head: { sha: 'head' }, base: { sha: 'base' }, labels: [{ name: 'automerge:ready' }] };
  const result = await reconcile({ context: {
    eventName: 'pull_request_target', payload: { action: 'edited', changes: { body: { from: 'None' } }, pull_request: pr },
  } });
  assert.equal(result.comments.some(comment => /automerge-invalidation/.test(comment.body)), true);
  assert.equal(result.removed.length, 1);
});

test('draft conversion invalidates owner admission before a later ready event can reuse it', async () => {
  const pr = { number: 1, head: { sha: 'head' }, base: { sha: 'base' }, labels: [{ name: 'automerge:ready' }] };
  const result = await reconcile({ context: {
    eventName: 'pull_request_target', payload: { action: 'converted_to_draft', pull_request: pr },
  } });
  assert.equal(result.comments.some(comment => /automerge-invalidation/.test(comment.body)), true);
  assert.equal(result.removed.length, 1);
});

test('requested-changes review invalidates owner admission on trusted reconciliation', async () => {
  const result = await reconcile({ reviews: [
    { id: 1, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED' },
  ] });
  assert.equal(result.comments.some(comment => /automerge-invalidation/.test(comment.body)), true);
  assert.equal(result.removed.length, 1);
});

test('removing a hold label still invalidates the earlier owner admission', async () => {
  const pr = { number: 1, head: { sha: 'head' }, base: { sha: 'base' }, labels: [{ name: 'automerge:ready' }] };
  const result = await reconcile({ context: {
    eventName: 'pull_request_target',
    payload: { action: 'unlabeled', label: { name: 'production-gate' },
      pull_request: pr },
  } });
  assert.equal(result.comments.some(comment => /automerge-invalidation/.test(comment.body)), true);
  assert.equal(result.removed.length, 1);
});

test('holds, requested changes, and unresolved review threads cancel a queued request', async () => {
  const hold = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    pr: { labels: [{ name: 'automerge:ready' }, { name: 'production-gate' }] } });
  assert.deepEqual(hold.mutations, ['disable']);
  const changes = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    reviews: [
      { id: 1, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED' },
      { id: 2, user: { login: 'reviewer' }, state: 'COMMENTED' },
    ] });
  assert.deepEqual(changes.mutations, ['disable']);
  const approved = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    reviews: [
      { id: 1, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED' },
      { id: 2, user: { login: 'reviewer' }, state: 'APPROVED' },
    ] });
  assert.deepEqual(approved.mutations, []);
  const dismissed = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    reviews: [
      { id: 1, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED' },
      { id: 2, user: { login: 'reviewer' }, state: 'DISMISSED' },
    ] });
  assert.deepEqual(dismissed.mutations, []);
  const unresolved = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    unresolved: true });
  assert.deepEqual(unresolved.mutations, ['disable']);
});

test('a comment-only review does not clear an outstanding changes-requested hold', async () => {
  const result = await reconcile({ enabled: true, protectionVerified: true, queued: true,
    reviews: [
      { id: 1, user: { login: 'reviewer' }, state: 'CHANGES_REQUESTED' },
      { id: 2, user: { login: 'reviewer' }, state: 'COMMENTED' },
    ] });
  assert.deepEqual(result.mutations, ['disable']);
});

test('owner label event records the exact current head, base, and description', async () => {
  const pr = { number: 1, head: { sha: 'head' }, base: { sha: 'base' },
    body: '## Merge dependencies\nNone' };
  const result = await reconcile({ context: {
    eventName: 'pull_request_target',
    payload: { action: 'labeled', label: { name: 'automerge:ready' },
      sender: { login: 'owner' }, pull_request: pr },
  } });
  assert.equal(result.comments.some(comment => comment.body.includes(
    '<!-- automerge-admission head=head base=base body=')) , true);
  assert.equal(result.comments.some(comment => /automerge-invalidation/.test(comment.body)), false);
  assert.equal(result.removed.length, 0);
});

async function publishReview({ currentHead = 'head', currentBase = 'base', reportHead = 'head', reportBase = 'base',
  decision = 'pass', statusError = false } = {}) {
  const statuses = [];
  const comments = [];
  const failures = [];
  const warnings = [];
  const report = { decision, reviewed_head_sha: reportHead, reviewed_base_sha: reportBase,
    summary: decision === 'pass' ? 'No actionable findings' : 'Changes requested',
    findings: decision === 'pass' ? [] : ['Fix the finding'] };
  await new AsyncFunction('github', 'context', 'core', 'process', 'require', reviewPublisher)(
    { rest: {
      pulls: { get: async () => ({ data: { state: 'open', draft: false,
        head: { sha: currentHead, repo: { full_name: 'owner/repo' } },
        base: { sha: currentBase, ref: 'main', repo: { full_name: 'owner/repo' } } } }) },
      issues: { createComment: async args => comments.push(args.body) },
      repos: { createCommitStatus: async args => {
        if (statusError) throw new Error('status publication failed');
        statuses.push(args);
      } },
    } },
    { repo: { owner: 'owner', repo: 'repo' }, payload: { pull_request: { number: 1 } },
      runId: 1, serverUrl: 'https://fixture.invalid' },
    { warning: message => warnings.push(message), setFailed: failure => failures.push(failure) },
    { env: { REVIEW_REPORT: JSON.stringify(report), REVIEW_JOB_RESULT: 'success', PR_NUMBER: '1',
      PR_HEAD_SHA: reportHead, PR_BASE_SHA: reportBase, MERGE_TOKEN: 'fixture-token' } },
    nodeRequire,
  );
  return { statuses, comments, failures, warnings };
}

async function resolveReviewTarget({ pr = {}, missing = false, dispatch = false } = {}) {
  const outputs = {};
  const failures = [];
  const livePR = {
    number: 1, state: 'open', draft: false,
    head: { sha: 'head', repo: { full_name: 'owner/repo' } },
    base: { sha: 'base', ref: 'main', repo: { full_name: 'owner/repo' } },
    ...pr,
  };
  const github = { rest: { pulls: { get: async () => ({ data: livePR }) } } };
  const context = { repo: { owner: 'owner', repo: 'repo' },
    eventName: dispatch ? 'repository_dispatch' : 'pull_request_target',
    payload: dispatch ? { client_payload: { pr_number: missing ? undefined : 1 } } :
      { pull_request: missing ? undefined : livePR } };
  await new AsyncFunction('github', 'context', 'core', reviewTargetResolver)(github, context, {
    setOutput: (key, value) => { outputs[key] = value; },
    setFailed: message => { failures.push(message); },
    info() {},
  });
  return { outputs, failures };
}

test('PR event review pins the current head and base', async () => {
  const { outputs, failures } = await resolveReviewTarget();
  assert.deepEqual(outputs, { eligible: 'true', number: '1', head_sha: 'head', base_sha: 'base' });
  assert.deepEqual(failures, []);
});

test('main push dispatches per-PR reviews and dispatch resolves the live revision', async () => {
  const workflow = readFileSync(new URL('../../.github/workflows/codex-review.yml', import.meta.url), 'utf8');
  assert.match(workflow, /  push:\n    branches: \[main\]/);
  assert.match(workflow, /  repository_dispatch:\n    types: \[codex-review-main-advanced\]/);
  assert.match(workflow, /createDispatchEvent/);
  const { outputs, failures } = await resolveReviewTarget({ dispatch: true });
  assert.deepEqual(outputs, { eligible: 'true', number: '1', head_sha: 'head', base_sha: 'base' });
  assert.deepEqual(failures, []);
  assert.equal((await resolveReviewTarget({ dispatch: true, missing: true })).failures.length, 1);
});

test('PR event review skips drafts, forks, and non-main bases', async () => {
  assert.equal((await resolveReviewTarget({ missing: true })).failures.length, 1);
  assert.equal((await resolveReviewTarget({ pr: { draft: true } })).outputs.eligible, 'false');
  assert.equal((await resolveReviewTarget({ pr: { head: { sha: 'head', repo: { full_name: 'other/repo' } } } })).outputs.eligible, 'false');
  assert.equal((await resolveReviewTarget({ pr: { base: { sha: 'base', ref: 'release', repo: { full_name: 'owner/repo' } } } })).outputs.eligible, 'false');
  const workflow = readFileSync(new URL('../../.github/workflows/codex-review.yml', import.meta.url), 'utf8');
  assert.doesNotMatch(workflow, /workflow_dispatch:/);
  const coordinatorWorkflow = readFileSync(new URL('../../.github/workflows/auto-merge.yml', import.meta.url), 'utf8');
  assert.doesNotMatch(coordinatorWorkflow, /workflow_dispatch:/);
});

test('Codex review publisher records exact SHA pair and result', async () => {
  const { statuses, comments, failures } = await publishReview();
  assert.equal(statuses[0].state, 'success');
  assert.equal(statuses[0].description, 'PASS base=base');
  assert.equal(comments[0].includes(reviewRecord().split('\n')[0]), true);
  assert.deepEqual(failures, []);
});

test('Codex review publisher keeps a changed PR gate closed without failing a main job', async () => {
  const { statuses, comments, failures, warnings } = await publishReview({ currentHead: 'different-head' });
  assert.equal(statuses[0].state, 'failure');
  assert.equal(comments[0].includes('"state": "failure"'), true);
  assert.deepEqual(failures, []);
  assert.equal(warnings.length, 1);
});

test('Codex review publisher keeps requested changes on the exact PR gate', async () => {
  const { statuses, comments, failures, warnings } = await publishReview({ decision: 'changes_requested' });
  assert.equal(statuses[0].state, 'failure');
  assert.equal(comments[0].includes('"decision": "changes_requested"'), true);
  assert.deepEqual(failures, []);
  assert.equal(warnings.length, 1);
});

test('Codex review publisher still fails the job if status publication fails', async () => {
  await assert.rejects(publishReview({ statusError: true }), /status publication failed/);
});

test('Codex API review job has no repository checkout and uses the screened patch artifact', () => {
  const workflow = readFileSync(new URL('../../.github/workflows/codex-review.yml', import.meta.url), 'utf8');
  const prepare = workflow.split('  prepare-patch:\n')[1].split('  review:\n')[0];
  const reviewJob = workflow.split('  review:\n')[1].split('  publish-review-status:\n')[0];
  assert.match(prepare, /actions\/checkout@/);
  assert.match(prepare, /500000/);
  assert.match(prepare, /PRIVATE KEY/);
  assert.match(prepare, /visa-trouble/);
  assert.match(prepare, /actions\/upload-artifact@/);
  assert.doesNotMatch(reviewJob, /actions\/checkout@/);
  assert.match(reviewJob, /actions\/download-artifact@/);
  assert.match(reviewJob, /openai\/codex-action@/);
});

test('patch screen rejects sensitive content in additions, removals, and context', () => {
  const workflow = readFileSync(new URL('../../.github/workflows/codex-review.yml', import.meta.url), 'utf8');
  const start = workflow.indexOf('          import pathlib\n');
  const end = workflow.indexOf('          PY\n', start);
  assert.ok(start >= 0 && end > start);
  const scanner = workflow.slice(start, end).split('\n').map(line => line.slice(10)).join('\n');
  const dir = mkdtempSync(join(tmpdir(), 'codex-review-screen-'));
  try {
    const path = join(dir, 'pr.diff');
    const result = (patch, repo = 'web2boomer/visa-trouble') => {
      writeFileSync(path, patch);
      return spawnSync('python3', ['-c', scanner, path, repo], { encoding: 'utf8' });
    };
    assert.equal(result('diff --git a/app/x b/app/x\n+const answer = 42;\n').status, 0);
    const fictionalAddress = 'client' + String.fromCharCode(64) + 'example.com';
    assert.equal(result('diff --git a/app/x b/app/x\n+' + fictionalAddress + '\n').status, 1);
    assert.equal(result('diff --git a/app/x b/app/x\n-' + fictionalAddress + '\n').status, 1);
    assert.equal(result('diff --git a/app/x b/app/x\n ' + fictionalAddress + '\n').status, 1);
    assert.equal(result('diff --git a/app/x b/app/x\n token = "abcdefghijklmnop"\n', 'web2boomer/mission-control').status, 1);
    assert.equal(result('diff --git a/app/.env b/app/.env\n+API_KEY=plaincredentialvalue\n', 'web2boomer/mission-control').status, 1);
    assert.equal(result('diff --git a/app/.env b/app/.env\n-API_KEY=plaincredentialvalue\n', 'web2boomer/mission-control').status, 1);
    assert.equal(result('diff --git a/app/config.yml b/app/config.yml\n password: plaincredentialvalue\n', 'web2boomer/mission-control').status, 1);
    assert.equal(result('diff --git a/script/test/x b/script/test/x\n+      MERGE_TOKEN: options.mergeTokenMissing ? \'\' : \'fixture-token\',\n', 'web2boomer/mission-control').status, 0);
    assert.equal(result('diff --git a/script/test/x b/script/test/x\n       MERGE_TOKEN: options.mergeTokenMissing ? \'\' : \'fixture-token\',\n', 'web2boomer/mission-control').status, 0);
    assert.equal(result('diff --git a/script/test/x b/script/test/x\n+    assert.equal(result("+API_KEY=plaincredentialvalue").status, 1);\n', 'web2boomer/mission-control').status, 0);
    assert.equal(result('diff --git a/private/case-history.txt b/private/case-history.txt\n').status, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
