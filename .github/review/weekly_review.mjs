import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const BACKLOG = path.join(ROOT, 'BACKLOG.md');
const STATE = path.join(ROOT, '.github', 'review', 'state.json');
const REVIEW_PREFIX = 'review/weekly-';
const BASELINE_TAG = 'weekly-review-baseline';
const BASE_BRANCH = process.env.REVIEW_BASE_BRANCH || 'main';
const REVIEW_ENGINE = process.env.REVIEW_ENGINE || 'github-copilot-cli';
const TODAY = new Date().toISOString().slice(0, 10);
const NOW = new Date().toISOString();
const MAX_CONTEXT_CHARS = 180_000;
const MAX_FILE_CHARS = 24_000;
const MAX_BACKLOG_CHARS = 35_000;
const MAX_MERGED_PRS = 100;

const TEXT_SUFFIXES = new Set([
  '.py', '.pyi', '.sql', '.toml', '.yaml', '.yml', '.json', '.md', '.sh', '.ps1', '.ini', '.cfg',
]);
const SKIP_PARTS = new Set([
  '.git', '.venv', 'venv', 'node_modules', 'dist', 'build', 'artifacts', 'data', 'models', '__pycache__',
]);
const SELF_FILES = new Set([
  'BACKLOG.md',
  '.github/review/weekly_review.mjs',
  '.github/review/state.json',
  '.github/workflows/weekly-review.yml',
]);

function run(command, args = [], { allowFailure = false, maxBuffer = 20 * 1024 * 1024 } = {}) {
  try {
    return execFileSync(command, args, {
      cwd: ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer,
      env: process.env,
    }).trim();
  } catch (error) {
    if (allowFailure) return '';
    const stderr = error?.stderr?.toString?.() || '';
    const stdout = error?.stdout?.toString?.() || '';
    throw new Error(`${command} ${args.join(' ')} failed\n${stderr || stdout}`.trim());
  }
}

function gh(...args) { return run('gh', args); }
function git(...args) { return run('git', args); }

function unresolvedReview() {
  const raw = gh('pr', 'list', '--state', 'open', '--limit', '100', '--json', 'number,headRefName,url');
  const prs = JSON.parse(raw || '[]');
  const openReviews = prs.filter((pr) => String(pr.headRefName || '').startsWith(REVIEW_PREFIX));
  if (openReviews.length) return `open weekly review PR exists: ${openReviews.map((pr) => `#${pr.number}`).join(', ')}`;
  const refs = run('git', ['ls-remote', '--heads', 'origin', `refs/heads/${REVIEW_PREFIX}*`], { allowFailure: true });
  return refs ? 'weekly review branch still exists on origin' : null;
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { return {}; }
}

function commitExists(sha) {
  return Boolean(sha) && spawnSync('git', ['cat-file', '-e', `${sha}^{commit}`], { cwd: ROOT }).status === 0;
}

function baselineFromStateOrTag(state) {
  const fromState = String(state.last_reviewed_target_sha || '').trim();
  if (commitExists(fromState)) return fromState;
  const fromTag = run('git', ['rev-parse', '-q', '--verify', `refs/tags/${BASELINE_TAG}`], { allowFailure: true });
  return commitExists(fromTag) ? fromTag : null;
}

function interesting(file) {
  const normalized = file.replaceAll('\\', '/');
  if (SELF_FILES.has(normalized)) return false;
  const parts = normalized.split('/');
  if (parts.some((part) => SKIP_PARTS.has(part))) return false;
  const name = path.basename(normalized).toLowerCase();
  return name.startsWith('dockerfile') || TEXT_SUFFIXES.has(path.extname(name));
}

function priority(file) {
  const parts = file.split('/');
  if (parts.some((part) => ['src', 'tests', 'sql', 'scripts', 'application', 'ingestion'].includes(part))) return 0;
  if (['pyproject.toml', 'requirements.txt', 'Dockerfile', 'docker-compose.yml', 'docker-compose.yaml'].includes(path.basename(file))) return 1;
  if (['ARCHITECTURE.md', 'METHODOLOGY.md', 'OPERATIONS.md', 'README.md', 'AGENTS.md'].includes(path.basename(file))) return 2;
  return 3;
}

function truncate(text, maxChars) {
  if (text.length <= maxChars) return text;
  const half = Math.floor(maxChars / 2);
  return `${text.slice(0, half)}\n\n... [truncated] ...\n\n${text.slice(-half)}`;
}

function collectContext(base, target) {
  const rawFiles = base ? git('diff', '--name-only', `${base}..${target}`).split('\n') : git('ls-files').split('\n');
  const files = rawFiles.filter(Boolean).filter(interesting).sort((a, b) => priority(a) - priority(b) || a.localeCompare(b));
  const sections = [];
  let used = 0;
  for (const file of files) {
    if (used >= MAX_CONTEXT_CHARS) break;
    let content = '';
    if (base) content = run('git', ['diff', '--unified=40', `${base}..${target}`, '--', file], { allowFailure: true });
    else {
      try { content = fs.readFileSync(path.join(ROOT, file), 'utf8'); } catch { continue; }
    }
    if (!content) continue;
    let section = `\n===== ${base ? 'DIFF' : 'FILE'}: ${file} =====\n${truncate(content, MAX_FILE_CHARS)}\n`;
    section = section.slice(0, MAX_CONTEXT_CHARS - used);
    sections.push(section);
    used += section.length;
  }
  return { text: sections.join(''), files };
}

function backlogText() {
  try { return fs.readFileSync(BACKLOG, 'utf8'); } catch { return ''; }
}

function backlogSummary(text) {
  if (!text) return '(BACKLOG.md does not exist yet)';
  if (text.length <= MAX_BACKLOG_CHARS) return text;
  const kept = [];
  let remaining = 0;
  for (const line of text.split('\n')) {
    if (/^#{1,4}\s+.*(?:PR[- #]?\d+|Merged|Completed|Backlog|Active)/i.test(line)) { kept.push(line); remaining = 6; }
    else if (remaining > 0) { kept.push(line); remaining -= 1; }
  }
  return truncate(kept.join('\n'), MAX_BACKLOG_CHARS);
}

function mergedSince(lastReviewedAt) {
  const raw = gh('pr', 'list', '--state', 'merged', '--limit', String(MAX_MERGED_PRS), '--json', 'number,title,mergedAt,body,url');
  const prs = JSON.parse(raw || '[]');
  return lastReviewedAt ? prs.filter((pr) => String(pr.mergedAt || '') > lastReviewedAt) : prs;
}

function extractJson(text) {
  const stripped = text.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/i, '').trim();
  try { return JSON.parse(stripped); } catch {
    const first = stripped.indexOf('{');
    const last = stripped.lastIndexOf('}');
    if (first >= 0 && last > first) return JSON.parse(stripped.slice(first, last + 1));
    throw new Error('Copilot did not return a JSON object');
  }
}

function callCopilot(repoContext, existingBacklog, mergedPrs, base, target) {
  const prompt = `You are a conservative senior software and quantitative-systems reviewer.
Repository: ${process.env.GITHUB_REPOSITORY || '(unknown)'}
Review date: ${TODAY}
Baseline commit: ${base || 'FIRST FULL REVIEW'}
Target commit: ${target}

Review only evidence contained below. Return JSON only, without Markdown fences.
Only report HIGH or MEDIUM findings. Do not report style, naming, formatting, or speculative design-pattern opportunities.
Prioritize correctness, data leakage/look-ahead bias, reproducibility, statistical validity, failure handling, security,
concurrency, material test gaps, architecture, performance, maintainability, and refactoring that solves a concrete observed problem.
For quantitative code explicitly check train/test contamination, future-information leakage, walk-forward boundaries,
optimization/evaluation coupling, numerical safety, and reproducibility.
Every finding must fit one small, complete, atomic, reviewable pull request. Split broad work into independent findings.
Every finding requires concrete evidence, bounded scope, at least four precise testable acceptance criteria, and a stable lowercase fingerprint.
Deduplicate against all active and completed BACKLOG.md material. Omit anything already represented there.

Return exactly this shape:
{"findings":[{"severity":"HIGH|MEDIUM","type":"Correctness|Data leakage|Reproducibility|Statistical validity|Failure handling|Security|Concurrency|Testing|Architecture|Performance|Maintainability|Refactoring","title":"short atomic PR title","problem":"specific evidenced problem and impact","evidence":["path: concrete evidence"],"proposed_change":"bounded implementation approach","scope":["specific file/component change"],"acceptance_criteria":["testable criterion","testable criterion","testable criterion","testable criterion"],"fingerprint":"domain|component|problem"}],"merged_summaries":[{"number":123,"title":"PR title","summary":"one concise factual sentence describing the merged change"}]}

EXISTING BACKLOG FOR DEDUPLICATION:
${existingBacklog}

MERGED PRS SINCE PREVIOUS SUCCESSFUL REVIEW:
${JSON.stringify(mergedPrs).slice(0, 35_000)}

CODE/DIFF CONTEXT:
${repoContext || '(no selected code changes)'}`;
  const result = spawnSync('copilot', ['-p', prompt, '--no-ask-user'], { cwd: ROOT, encoding: 'utf8', env: process.env, maxBuffer: 20 * 1024 * 1024 });
  if (result.status !== 0) throw new Error(`Copilot CLI failed: ${(result.stderr || result.stdout || '').slice(-4000)}`);
  return extractJson(result.stdout || '');
}

function validateFindings(raw, existingBacklog) {
  if (!Array.isArray(raw)) return [];
  const lowerBacklog = existingBacklog.toLowerCase();
  const seen = new Set();
  const valid = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const severity = String(item.severity || '').toUpperCase();
    const fingerprint = String(item.fingerprint || '').trim().toLowerCase();
    const acceptance = Array.isArray(item.acceptance_criteria) ? item.acceptance_criteria.map(String).filter(Boolean) : [];
    const evidence = Array.isArray(item.evidence) ? item.evidence.map(String).filter(Boolean) : [];
    const scope = Array.isArray(item.scope) ? item.scope.map(String).filter(Boolean) : [];
    if (!['HIGH', 'MEDIUM'].includes(severity)) continue;
    if (!fingerprint || lowerBacklog.includes(fingerprint) || seen.has(fingerprint)) continue;
    if (!String(item.title || '').trim() || !String(item.problem || '').trim() || !String(item.proposed_change || '').trim()) continue;
    if (!evidence.length || !scope.length || acceptance.length < 4) continue;
    valid.push({ ...item, severity, fingerprint, evidence, scope, acceptance_criteria: acceptance });
    seen.add(fingerprint);
  }
  return valid;
}

function nextBacklogId(text) {
  const ids = [...text.matchAll(/\bPR[- #]?(\d+)\b/gi)].map((m) => Number(m[1]));
  return (ids.length ? Math.max(...ids) : 0) + 1;
}

function findingMarkdown(id, item) {
  const evidence = item.evidence.map((x) => `- ${x}`).join('\n');
  const scope = item.scope.map((x) => `- ${x}`).join('\n');
  const acceptance = item.acceptance_criteria.map((x) => `- [ ] ${x}`).join('\n');
  return `## PR-${id} -- ${item.title}\n\n**Severity:** ${item.severity}  \n**Type:** ${item.type || 'Maintainability'}  \n**Status:** BACKLOG  \n**Fingerprint:** \`${item.fingerprint}\`  \n**Detected:** ${TODAY}\n\n### Problem\n\n${item.problem}\n\n### Evidence\n\n${evidence}\n\n### Proposed change\n\n${item.proposed_change}\n\n### Scope\n\n${scope}\n\n### Acceptance criteria\n\n${acceptance}\n`;
}

function appendUpdates(existing, findings, mergedSummaries) {
  let current = existing || '# Backlog\n';
  let id = nextBacklogId(current);
  const ids = [];
  let additions = '';
  for (const item of findings) { ids.push(id); additions += `\n${findingMarkdown(id, item)}\n`; id += 1; }
  if (additions) {
    const completed = /^#\s+(?:Completed|Merged)(?:\s*\/\s*Merged)?\b/im.exec(current);
    current = completed ? `${current.slice(0, completed.index).trimEnd()}\n${additions}\n${current.slice(completed.index).trimStart()}` : `${current.trimEnd()}\n${additions}`;
  }
  const summaries = Array.isArray(mergedSummaries) ? mergedSummaries : [];
  const newRows = [];
  for (const item of summaries) {
    const number = Number(item?.number); const title = String(item?.title || '').trim(); const summary = String(item?.summary || '').trim();
    if (!Number.isInteger(number) || !title || !summary) continue;
    if (new RegExp(`\\bPR\\s+#${number}\\b`, 'i').test(current)) continue;
    newRows.push(`- PR #${number} -- **${title}**: ${summary}`);
  }
  if (newRows.length) {
    if (!/^#\s+(?:Completed|Merged)(?:\s*\/\s*Merged)?\b/im.test(current)) current = `${current.trimEnd()}\n\n# Completed / Merged\n`;
    current = `${current.trimEnd()}\n\n## Weekly review merged summary -- ${TODAY}\n\n${newRows.join('\n')}\n`;
  }
  return { text: current, ids };
}

function configureGit() {
  git('config', 'user.name', 'github-actions[bot]');
  git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com');
}

function advanceBaseline(target) {
  configureGit();
  git('tag', '-f', BASELINE_TAG, target);
  git('push', '--force', 'origin', `refs/tags/${BASELINE_TAG}`);
}

function openReviewPr(target, base, newBacklog, ids, fileCount) {
  const branch = `${REVIEW_PREFIX}${TODAY}`;
  configureGit();
  git('checkout', '-b', branch, target);
  fs.writeFileSync(BACKLOG, newBacklog, 'utf8');
  fs.mkdirSync(path.dirname(STATE), { recursive: true });
  fs.writeFileSync(STATE, `${JSON.stringify({ last_reviewed_target_sha: target, last_reviewed_at: NOW, last_review_engine: REVIEW_ENGINE }, null, 2)}\n`, 'utf8');
  git('add', 'BACKLOG.md', '.github/review/state.json');
  git('commit', '-m', `docs(review): weekly repository review ${TODAY}`);
  git('push', '-u', 'origin', branch);
  const body = ['Automated weekly repository review.', '', `- Review baseline: \`${base || 'FIRST FULL REVIEW'}\``, `- Review target: \`${target}\``, `- Review engine: \`${REVIEW_ENGINE}\``, `- New backlog items: ${ids.length ? ids.map((id) => `PR-${id}`).join(', ') : 'none'}`, `- Files considered: ${fileCount}`, '', 'Implementation work remains in separate atomic PRs.'].join('\n');
  gh('pr', 'create', '--base', BASE_BRANCH, '--head', branch, '--title', `Weekly repository review -- ${TODAY}`, '--body', body);
}

function main() {
  const blocked = unresolvedReview();
  if (blocked) { console.log(`SKIP: ${blocked}`); return; }
  const target = git('rev-parse', 'HEAD');
  const state = loadState();
  const base = baselineFromStateOrTag(state);
  const { text: context, files } = collectContext(base, target);
  const merged = mergedSince(state.last_reviewed_at);
  if (base && !context.trim() && merged.length === 0) { advanceBaseline(target); console.log('No code changes or merged PR summaries; baseline advanced.'); return; }
  const existing = backlogText();
  const response = callCopilot(context, backlogSummary(existing), merged, base, target);
  const findings = validateFindings(response.findings, existing);
  const updated = appendUpdates(existing, findings, response.merged_summaries);
  if (updated.text === existing) { advanceBaseline(target); console.log('No new HIGH/MEDIUM findings or merged-PR summary changes; no review PR created.'); return; }
  openReviewPr(target, base, updated.text, updated.ids, files.length);
}

try { main(); } catch (error) { console.error(`ERROR: ${error?.stack || error}`); process.exitCode = 1; }
