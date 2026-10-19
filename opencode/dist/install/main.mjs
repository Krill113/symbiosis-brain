import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { inspectInstall, planInstall, applyInstall, removeOwned } from './cli.mjs';
async function main() {
    let request;
    try {
        request = JSON.parse(await new Promise((resolve, reject) => {
            let text = '';
            process.stdin.setEncoding('utf8');
            process.stdin.on('data', (chunk) => { text += chunk; if (text.length > 1_000_000)
                reject(new Error('request-too-large')); });
            process.stdin.on('end', () => resolve(text));
            process.stdin.on('error', reject);
        }));
        if (!request || !['install', 'doctor', 'remove', 'plan'].includes(request.command))
            throw new Error('invalid-request');
        if (request.command === 'install' || request.command === 'plan') {
            if (!request.input)
                throw new Error('install-input-required');
            const plan = await planInstall(request.input);
            if (request.command === 'plan')
                process.stdout.write(`${JSON.stringify({ ok: true, planned: true, operationCount: plan.operations.length })}\n`);
            else {
                const receipt = await applyInstall(plan);
                process.stdout.write(`${JSON.stringify({ ok: true, status: 'installed', transactionId: receipt.transactionId })}\n`);
            }
        }
        else if (request.command === 'doctor') {
            const configDir = request.input?.configDir;
            if (!configDir)
                throw new Error('config-dir-required');
            const report = await inspectInstall(configDir);
            process.stdout.write(`${JSON.stringify({ ok: true, status: report.status, conflictCount: report.conflicts.length, diagnosticCount: report.diagnostics.length })}\n`);
            if (report.status === 'conflict' || report.status === 'incomplete')
                process.exitCode = 1;
        }
        else {
            const configDir = request.input?.configDir;
            if (!configDir)
                throw new Error('config-dir-required');
            const receiptPath = join(configDir, '.symbiosis-brain-install', 'receipt.json');
            const receipt = JSON.parse(await readFile(receiptPath, 'utf8'));
            const result = await removeOwned(receipt, configDir);
            process.stdout.write(`${JSON.stringify({ ok: true, status: result.status, conflictCount: result.conflicts.length })}\n`);
            if (result.status === 'conflict')
                process.exitCode = 1;
        }
    }
    catch (error) {
        const message = error instanceof Error ? error.message : 'installer-failed';
        const safeCode = /^[a-z0-9:_-]{1,160}$/i.test(message) ? message : 'installer-failed';
        process.stderr.write(`${JSON.stringify({ ok: false, error: safeCode })}\n`);
        process.exitCode = 1;
    }
}
void main();
