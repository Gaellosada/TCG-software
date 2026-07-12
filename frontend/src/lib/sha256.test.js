// @vitest-environment node
//
// Correctness of the pure-JS SHA-256 fallback: FIPS 180-4 known-answer vectors
// plus a cross-check against Node's WebCrypto/crypto over many lengths
// (including 55/56/57/63/64/65-byte block-boundary cases).

import { describe, it, expect } from 'vitest';
import { createHash } from 'node:crypto';
import { sha256HexFromBytes } from './sha256';

const enc = new TextEncoder();
const hex = (s) => sha256HexFromBytes(enc.encode(s));

describe('sha256HexFromBytes — known-answer vectors', () => {
  it('sha256("") ', () => {
    expect(hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });
  it('sha256("abc")', () => {
    expect(hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  });
  it('sha256(56-char multi-block vector)', () => {
    expect(hex('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq'))
      .toBe('248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1');
  });
  it('sha256(one million "a") — long multi-block', () => {
    expect(hex('a'.repeat(1000000)))
      .toBe('cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0');
  });
});

describe('sha256HexFromBytes — cross-check vs Node crypto', () => {
  it('matches Node for lengths spanning block boundaries', () => {
    for (const n of [0, 1, 2, 31, 32, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 200, 1000, 5000]) {
      const buf = Buffer.alloc(n);
      for (let i = 0; i < n; i++) buf[i] = (i * 31 + 7) & 0xff;
      const mine = sha256HexFromBytes(new Uint8Array(buf));
      const node = createHash('sha256').update(buf).digest('hex');
      expect(mine, `length ${n}`).toBe(node);
    }
  });

  it('matches Node for UTF-8 multi-byte content', () => {
    const s = 'ключ—曲線—😀 portfolio {"a":1,"b":[2,3]}';
    expect(sha256HexFromBytes(enc.encode(s)))
      .toBe(createHash('sha256').update(s, 'utf8').digest('hex'));
  });
});
