// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Runtime configuration. This placeholder ships with the build so the file always
// exists; the deploy overwrites it in S3 with real CloudFormation outputs. Empty
// values fall back to the build-time .env for local development - see src/config.js.
//
// Deployed shape:
//   window.__APP_CONFIG__ = {
//     apiUrl:             'https://api.example.com',
//     cognitoDomain:      'my-pool-domain',
//     clientId:           'abc123',
//     awsRegion:          'us-east-1',
//     deployedBackendUrl: 'https://bedrock-agentcore.us-east-1.amazonaws.com/...',
//     appName:            'DBOps Agent',
//   }
window.__APP_CONFIG__ = {}
