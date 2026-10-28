import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import process from 'node:process';
import { describe, expect, test } from 'vitest';

const settingsSource = readFileSync(
  resolve(process.cwd(), 'src/pages/ShipyardSettings.jsx'),
  'utf8',
);

describe('ShipyardSettings', () => {
  test('makes global project binding scope explicit', () => {
    expect(settingsSource).toContain('全局船号 / 工程号 / 制作图绑定');
    expect(settingsSource).toContain('所有仓库共享这组船号主数据');
    expect(settingsSource).toContain('仓库隔离适用于清单、票货、托盘、照片和区域定置');
  });
});
