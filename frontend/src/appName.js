// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Single source for the product name shown in the UI. Supplied at deploy time
// through the runtime config (see config.js); falls back to the default for
// local development.
import { config } from './config'

export const APP_NAME = config.appName
