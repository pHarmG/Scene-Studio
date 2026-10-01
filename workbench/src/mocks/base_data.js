/**
 * Base sample data loader for the browser (Vite JSON imports).
 *
 * The JSON files are verbatim copies of
 * `backend/fixtures/*.sample.json` (contract §7: the mocks
 * must consume the same JSON shapes as the Python domain).
 *
 * Node code (scripts/smoke.mjs) must NOT import this module; it reads the
 * same files with fs and calls the same factories.
 */

import registrySample from "./registry.sample.json";
import scenesSample from "./scenes.sample.json";
import discoverySample from "./discovery.sample.json";

/** @type {{registry: object, scenes: object, discovery: object}} */
export const baseData = {
  registry: registrySample,
  scenes: scenesSample,
  discovery: discoverySample,
};
