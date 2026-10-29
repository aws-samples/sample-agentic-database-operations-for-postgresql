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
  webAppUrl: import.meta.env.VITE_WEBAPP_URL,
  appName: import.meta.env.VITE_APP_NAME,
  resourcePrefix: import.meta.env.VITE_RESOURCE_PREFIX,
}

const defaults = {
  apiUrl: 'http://localhost:8084',
  appName: 'DBOps Agent (Beta)',
  // Matches the ResourcePrefix parameter default. Used to build the secret name
  // the operator is told to create, which the deployment's IAM policies authorise
  // by the same prefix.
  resourcePrefix: 'dbops',
}

const pick = (key) => runtime[key] || buildTime[key] || defaults[key] || ''

export const config = {
  apiUrl: pick('apiUrl'),
  cognitoDomain: pick('cognitoDomain'),
  clientId: pick('clientId'),
  awsRegion: pick('awsRegion'),
  webAppUrl: pick('webAppUrl'),
  appName: pick('appName'),
  resourcePrefix: pick('resourcePrefix'),
}

export default config
