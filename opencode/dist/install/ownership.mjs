import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { constants } from 'node:fs';
import { mkdir, open, readFile, realpath, rename, stat } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
export function hashText(value) {
    return value === null ? null : createHash('sha256').update(value).digest('hex');
}
export async function readOptional(path) {
    try {
        return await readFile(path, 'utf8');
    }
    catch (error) {
        if (error.code === 'ENOENT')
            return null;
        throw error;
    }
}
export async function durableWrite(path, data) {
    await mkdir(dirname(path), { recursive: true });
    const temp = `${path}.tmp-${process.pid}-${Date.now()}`;
    const handle = await open(temp, 'wx', 0o600);
    try {
        await handle.writeFile(data, 'utf8');
        await handle.sync();
    }
    finally {
        await handle.close();
    }
    await rename(temp, path);
    try {
        const directory = await open(dirname(path), constants.O_RDONLY);
        try {
            await directory.sync();
        }
        finally {
            await directory.close();
        }
    }
    catch (error) {
        const code = error.code;
        const unsupported = ['EINVAL', 'ENOSYS', 'ENOTSUP', 'EISDIR'].includes(code ?? '') || (process.platform === 'win32' && ['EPERM', 'EACCES'].includes(code ?? ''));
        if (!unsupported)
            throw error;
    }
}
export function conditionalCommit(path, replacement, expectedHash, pythonExe) {
    if (process.platform !== 'win32')
        throw new Error('conditional-commit-unsupported-platform');
    const code = 'from symbiosis_brain.adapters.opencode.conditional_replace import main; main()';
    const result = spawnSync(pythonExe, ['-c', code], {
        input: JSON.stringify({ path, expectedSha256: expectedHash, replacementBase64: replacement === null ? null : Buffer.from(replacement, 'utf8').toString('base64') }),
        encoding: 'utf8', windowsHide: true, timeout: 10_000, maxBuffer: 128 * 1024,
    });
    if (result.error || result.status !== 0) {
        const codeOut = result.stdout.trim();
        let reason = 'conditional-commit-failed';
        try {
            const response = JSON.parse(codeOut);
            if (response.code)
                reason = response.code;
        }
        catch { /* keep a fixed diagnostic */ }
        throw new Error(reason);
    }
    let response;
    try {
        response = JSON.parse(result.stdout);
    }
    catch {
        throw new Error('conditional-commit-invalid-response');
    }
    if (response.ok !== true)
        throw new Error('conditional-commit-failed');
}
export function conditionalRecover(path, pythonExe) {
    if (process.platform !== 'win32')
        throw new Error('conditional-commit-unsupported-platform');
    const code = 'from symbiosis_brain.adapters.opencode.conditional_replace import main; main()';
    const result = spawnSync(pythonExe, ['-c', code], {
        input: JSON.stringify({ operation: 'recover', path }), encoding: 'utf8', windowsHide: true, timeout: 10_000, maxBuffer: 128 * 1024,
    });
    if (result.error || result.status !== 0) {
        let reason = 'conditional-recovery-failed';
        try {
            const response = JSON.parse(result.stdout.trim());
            if (response.code)
                reason = response.code;
        }
        catch { /* fixed diagnostic */ }
        throw new Error(reason);
    }
    try {
        if (JSON.parse(result.stdout).ok !== true)
            throw new Error('conditional-recovery-failed');
    }
    catch {
        throw new Error('conditional-recovery-invalid-response');
    }
}
export async function acquireInstallLock(root, pythonExe, packageDir) {
    const lockPath = join(root, '.symbiosis-brain-install', 'lock');
    if (!pythonExe)
        throw new Error('installation-lock-python-not-configured');
    const expectedPackageDir = await realpath(packageDir);
    const helperPath = join(dirname(fileURLToPath(import.meta.url)), 'install_lock.py');
    const child = spawn(pythonExe, ['-u', helperPath, lockPath, expectedPackageDir], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    let stdout = '';
    let stderr = '';
    let lost = false;
    let releaseRequested = false;
    let processExited = false;
    let processExitCode = null;
    const pending = new Map();
    let ready = false;
    let lineBuffer = '';
    child.stdout.setEncoding('utf8').on('data', (chunk) => {
        stdout += chunk;
        lineBuffer += chunk;
        for (;;) {
            const end = lineBuffer.indexOf('\n');
            if (end < 0)
                break;
            const line = lineBuffer.slice(0, end).trim();
            lineBuffer = lineBuffer.slice(end + 1);
            if (line.startsWith('READY ')) {
                try {
                    const handshake = JSON.parse(line.slice(6));
                    const normalizePath = (value) => value.replaceAll('\\', '/').toLocaleLowerCase();
                    if (typeof handshake.package !== 'string' || normalizePath(handshake.package) !== normalizePath(expectedPackageDir))
                        lost = true;
                    else
                        ready = true;
                }
                catch {
                    lost = true;
                }
                continue;
            }
            let response;
            try {
                response = JSON.parse(line);
            }
            catch {
                lost = true;
                continue;
            }
            const request = pending.get(String(response.id));
            if (!request)
                continue;
            pending.delete(String(response.id));
            if (response.ok !== true) {
                const result = response.result;
                const detail = typeof response.error === 'string' ? response.error : 'installation-lock-operation-failed';
                request.reject(new Error(result?.code ?? result?.state ?? `${detail}:${String(response.operation ?? 'unknown')}:${String(response.path ?? '')}`));
            }
            else
                request.resolve(response);
        }
    });
    child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
    child.once('exit', (code) => {
        processExited = true;
        processExitCode = code;
        if (!releaseRequested)
            lost = true;
        for (const [id, request] of pending) {
            if (request.allowExitAck)
                continue;
            pending.delete(id);
            request.reject(new Error(`installation-lock-lost:${code ?? 'unknown'}`));
        }
    });
    child.once('close', (code) => {
        for (const [id, request] of pending) {
            pending.delete(id);
            request.reject(new Error(`installation-lock-lost:${code ?? 'unknown'}`));
        }
    });
    await new Promise((resolveReady, rejectReady) => {
        const timer = setTimeout(() => rejectReady(new Error('installation-lock-timeout')), 10_000);
        const check = () => {
            if (ready) {
                clearTimeout(timer);
                resolveReady();
            }
            else if (child.exitCode !== null) {
                clearTimeout(timer);
                rejectReady(new Error(stderr.includes('LOCKED') ? 'installation-locked' : 'installation-lock-failed'));
            }
        };
        child.stdout.on('data', check);
        child.once('error', (error) => { clearTimeout(timer); rejectReady(error); });
        child.once('exit', check);
    }).catch((error) => { child.kill(); throw error; });
    let sequence = 0;
    const rpc = (operation, payload = {}, allowRelease = false) => {
        if (!allowRelease)
            release.assertHeld();
        const id = String(++sequence);
        return new Promise((resolveResponse, rejectResponse) => {
            pending.set(id, { resolve: resolveResponse, reject: rejectResponse, allowExitAck: allowRelease });
            child.stdin.write(`${JSON.stringify({ id, operation, ...payload })}\n`, (error) => {
                if (error) {
                    pending.delete(id);
                    rejectResponse(error);
                }
            });
        });
    };
    const release = (() => {
        release.assertHeld();
        const exited = new Promise((resolveExit, rejectExit) => {
            if (processExited) {
                resolveExit(processExitCode);
                return;
            }
            const timer = setTimeout(() => rejectExit(new Error('installation-lock-release-timeout')), 10_000);
            child.once('exit', (code) => { clearTimeout(timer); resolveExit(code); });
            child.once('error', (error) => { clearTimeout(timer); rejectExit(error); });
        });
        releaseRequested = true;
        return (async () => {
            await rpc('release', {}, true);
            child.stdin.end();
            const code = await exited;
            if (code !== 0)
                throw new Error('installation-lock-release-failed');
        })();
    });
    release.assertHeld = () => {
        if (lost || processExited || releaseRequested)
            throw new Error('installation-lock-lost');
    };
    release.mkdir = async (path) => { await rpc('mkdir', { path }); };
    release.write = async (path, data, testGate) => { await rpc('write', { path, data: Buffer.from(data, 'utf8').toString('base64'), ...(testGate ? { testGate } : {}) }); };
    release.remove = async (path) => { await rpc('remove', { path }); };
    release.conditional = async (path, replacement, expectedHash, executable) => {
        const payload = { path, expectedSha256: expectedHash, replacementBase64: replacement === null ? null : Buffer.from(replacement, 'utf8').toString('base64') };
        const response = await rpc('conditional', { payload, pythonExe: executable });
        if (response.result?.ok !== true)
            throw new Error('conditional-commit-failed');
    };
    release.recover = async (path, executable) => {
        try {
            await stat(dirname(path));
        }
        catch (error) {
            if (error.code === 'ENOENT')
                return;
            throw error;
        }
        const response = await rpc('recover', { payload: { operation: 'recover', path }, pythonExe: executable });
        if (response.result?.ok !== true)
            throw new Error('conditional-recovery-failed');
    };
    if (!child.pid)
        throw new Error('installation-lock-process-missing');
    release.holderPid = child.pid;
    return release;
}
export async function saveJournal(path, journal) {
    journal.updatedAt = new Date().toISOString();
    await durableWrite(path, `${JSON.stringify(journal, null, 2)}\n`);
}
export async function exists(path) {
    try {
        await stat(path);
        return true;
    }
    catch {
        return false;
    }
}
