/**
 * Entry point: compile one FDL document with Orquestra's compiler, then execute
 * the plan with HIS interpreter against a light fake NodeContext.
 *
 * stdin  {"doc": …, "inputs": {…}, "rpcUrl": "…", "projects": {"<projectId>": {idl, programId, projectName}}}
 * stdout one `__ORQUESTRA_BRIDGE_RESULT__` line:
 *          {"compile": <CompileResult>}                      — never compiled, so never ran
 *          {"compile": …, "run": <RunResult>}                — his RunResult verbatim
 *
 * READS ONLY. The nodes this reaches fetch a blockhash and simulate; nothing
 * here signs a transaction or submits one. `solana.compose_transaction@1`
 * returns unsigned v0 bytes and, when the flow asks, a simulation of them.
 */

import { loadEngine, emit, readRequest, fail } from './engine.ts'
import { fakeContext, type ProjectIdlRow } from './fake-context.ts'

interface RunRequest {
  doc: unknown
  inputs: Record<string, unknown>
  rpcUrl: string
  projects: Record<string, ProjectIdlRow>
}

try {
  const { compile, run } = await loadEngine()
  const request = await readRequest<RunRequest>()

  const compiled = await compile(request.doc)
  if (!compiled.ok) {
    emit({ compile: compiled })
    process.exit(0)
  }

  const ctx = fakeContext(request.rpcUrl, request.projects ?? {})
  emit({ compile: compiled, run: await run(compiled.plan, request.inputs ?? {}, ctx) })
} catch (error) {
  fail(error)
}
