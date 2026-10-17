/**
 * The live feature list: every ui/src/v8/features/<id>/index.ts(x) exporting `feature`.
 * Eager on purpose: index modules are tiny (metadata + lazy element); pages are their own chunks.
 */
import { collectFeatures } from './discovery';

export const DISCOVERY = collectFeatures(import.meta.glob('../features/*/index.{ts,tsx}', { eager: true }));
