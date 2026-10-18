// Access to the repository the site documents. Pages embed real files through RepoFile and
// RepoDoc instead of copying them, so the site cannot drift from the code.
import { existsSync, readFileSync } from 'node:fs';
import { extname, resolve, sep } from 'node:path';
import siteConfig from '../../site.config.json';

/** The repository root: `astro` runs from site/, one level below it. */
export const REPO_ROOT = resolve(process.cwd(), '..');
if (!existsSync(resolve(REPO_ROOT, 'pyproject.toml'))) {
  throw new Error(`site: expected the EBS repository at ${REPO_ROOT}; run astro from site/`);
}

export const REPO_URL: string = siteConfig.repo;
export const BASE: string = siteConfig.base;

/** A site-internal link: `link('/start/quickstart/')` → `/ebs/start/quickstart/`. */
export function link(path: string): string {
  return `${BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

/** The GitHub URL of a repository file or directory. */
export function repoUrl(path: string): string {
  return `${REPO_URL}/blob/main/${path}`;
}

/** The text of a repository file; throws (failing the build) if it is missing. */
export function readRepoFile(path: string): string {
  const full = resolve(REPO_ROOT, path);
  if (!full.startsWith(REPO_ROOT + sep)) {
    throw new Error(`site: ${path} is outside the repository`);
  }
  if (!existsSync(full)) {
    throw new Error(`site: a page embeds ${path}, which does not exist; update the page`);
  }
  return readFileSync(full, 'utf-8');
}

const LANGS: Record<string, string> = {
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.json': 'json',
  '.py': 'python',
  '.sh': 'bash',
  '.sv': 'systemverilog',
  '.v': 'verilog',
  '.csv': 'csv',
  '.md': 'md',
  '.lua': 'lua',
  '.xml': 'xml',
  '.service': 'ini',
  '.timer': 'ini',
  '.example': 'toml',
};

/** The highlighting language of a file, from its name. */
export function languageOf(path: string): string {
  const name = path.split('/').pop() ?? path;
  if (name === 'Makefile' || name.endsWith('.mk')) return 'make';
  if (name === 'Containerfile' || name === 'Dockerfile') return 'dockerfile';
  return LANGS[extname(name)] ?? 'text';
}

/**
 * Lines `from`..`to` (1-based, inclusive) of `text`. `range` is "12-30", "12-" or "12".
 */
export function sliceLines(text: string, range: string | undefined): string {
  if (!range) return text.replace(/\n$/, '');
  const match = /^(\d+)(?:-(\d*))?$/.exec(range);
  if (!match) throw new Error(`site: invalid line range ${JSON.stringify(range)}`);
  const lines = text.replace(/\n$/, '').split('\n');
  const from = Number(match[1]);
  const to = match[2] === undefined ? from : match[2] === '' ? lines.length : Number(match[2]);
  if (from < 1 || to > lines.length || from > to) {
    throw new Error(`site: line range ${range} is outside the file (${lines.length} lines)`);
  }
  return lines.slice(from - 1, to).join('\n');
}
