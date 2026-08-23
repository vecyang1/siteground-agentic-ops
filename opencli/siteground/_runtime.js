// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';


const userRuntimeRequire = createRequire(join(homedir(), '.opencli', 'package.json'));
const registryUrl = pathToFileURL(userRuntimeRequire.resolve('@jackwener/opencli/registry')).href;
const errorsUrl = pathToFileURL(userRuntimeRequire.resolve('@jackwener/opencli/errors')).href;
const registry = await import(registryUrl);
const errors = await import(errorsUrl);


export const { cli, Strategy } = registry;
export const { ArgumentError, CommandExecutionError, EmptyResultError } = errors;
