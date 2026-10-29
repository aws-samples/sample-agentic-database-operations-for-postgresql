// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Runtime configuration.
 *
 * The built bundle carries no environment-specific values. `index.html` loads
 * `/config.js` as a classic script, which runs *before* the deferred module
 * bundle, so `window.__APP_CONFIG__` is always populated by the time this module
 * is evaluated. The deploy writes real values into that one small file instead
 * of rebuilding the app or string-substituting the minified bundle.
 *
 * Precedence: deployed `window.__APP_CONFIG__` -> build-time `.env` (local dev)
 * -> hardcoded default.
 */

const runtime = (typeof window !== 'undefined' && window.__APP_CONFIG__) || {}

// Read every `import.meta.env.VITE_*` as a *static* member access. Vite inlines
// these at build time; a computed lookup like `import.meta.env[key]` is not
// guaranteed to be replaced in a production build.
const buildTime = {
  apiUrl: import.meta.env.VITE_API_URL,
  cognitoDomain: import.meta.env.VITE_COGNITO_DOMAIN,
  clientId: import.meta.env.VITE_CLIENT_ID,
  awsRegion: import.meta.env.VITE_AWS_REGION,
  deployedBackendUrl: import.meta.env.VITE_DEPLOYED_BACKEND_URL,
  localBackendUrl: import.meta.env.VITE_LOCAL_BACKEND_URL,
  appName: import.meta.env.VITE_APP_NAME,
}

const defaults = {
  localBackendUrl: 'http://localhost:8084',
  appName: 'DBOps Agent (Beta)',
}

const pick = (key) => runtime[key] || buildTime[key] || defaults[key] || ''

export const config = {
  apiUrl: pick('apiUrl'),
  cognitoDomain: pick('cognitoDomain'),
  clientId: pick('clientId'),
  awsRegion: pick('awsRegion'),
  deployedBackendUrl: pick('deployedBackendUrl'),
  localBackendUrl: pick('localBackendUrl'),
  appName: pick('appName'),
}

export default config
