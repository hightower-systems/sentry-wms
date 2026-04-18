/**
 * V-046: verify the SRI plugin stays wired into the Vite build config.
 *
 * This is a static-file assertion rather than a full build test: running
 * vite build in the unit-test harness is slow, but losing the SRI plugin
 * would silently ship a bundle without integrity attributes, so the
 * regression guard still earns its keep as a lint-level check.
 *
 * Manual verification: run `npm run build` and grep dist/index.html for
 * `integrity="sha384-"` on every script and stylesheet tag.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

describe('vite.config.js (V-046 SRI)', () => {
  const config = readFileSync(join(process.cwd(), 'vite.config.js'), 'utf8');

  it('imports the sri plugin from vite-plugin-sri3', () => {
    expect(config).toMatch(/from ['"]vite-plugin-sri3['"]/);
  });

  it('invokes sri() inside the plugins array', () => {
    expect(config).toMatch(/sri\s*\(/);
  });

  it('package.json pins vite-plugin-sri3 as a devDependency', () => {
    const pkg = JSON.parse(
      readFileSync(join(process.cwd(), 'package.json'), 'utf8'),
    );
    expect(pkg.devDependencies?.['vite-plugin-sri3']).toBeDefined();
  });
});
