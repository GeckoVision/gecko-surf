/**
 * Loads Orquestra's REAL flow engine from a checkout on disk.
 *
 * Nothing here reimplements his compiler, his schema or his interpreter — this
 * module only locates them. The checkout path arrives as `ORQUESTRA_ROOT` in the
 * environment rather than being written into this file, so the bridge is a
 * function of where his repo actually is.
 *
 * The imports are dynamic because the path is dynamic; `flow-engine/index.ts`
 * is imported for its side effect (each node module calls `registerNode` at
 * import time), which is what makes `compile()`'s node-type check and the
 * interpreter's registry lookup see all 15 node types.
 */

export interface Engine {
  compile: (doc: unknown, opts?: { skipNodeTypeCheck?: boolean }) => Promise<any>
  run: (plan: unknown, inputs: Record<string, unknown>, ctx: unknown) => Promise<any>
}

export function orquestraRoot(): string {
  const root = process.env.ORQUESTRA_ROOT
  if (!root) {
    throw new Error('ORQUESTRA_ROOT is not set — the bridge must be told where the Orquestra checkout is')
  }
  return root.replace(/\/+$/, '')
}

export async function loadEngine(): Promise<Engine> {
  const engineDir = `${orquestraRoot()}/packages/worker/src/flow-engine`
  await import(`${engineDir}/index.ts`) // side effect: registers all 15 node types
  const compiler = await import(`${engineDir}/compiler.ts`)
  const interpreter = await import(`${engineDir}/interpreter.ts`)
  return { compile: compiler.compile, run: interpreter.run }
}

/** stdout is a wire, not a log. Everything the bridge returns rides one marked line. */
export const RESULT_MARKER = '__ORQUESTRA_BRIDGE_RESULT__'

export function emit(payload: unknown): void {
  console.log(`${RESULT_MARKER} ${JSON.stringify(payload)}`)
}

export async function readRequest<T>(): Promise<T> {
  return JSON.parse(await Bun.stdin.text()) as T
}

export function fail(error: unknown): never {
  const message = error instanceof Error ? error.message : String(error)
  const stack = error instanceof Error ? error.stack : undefined
  emit({ bridgeError: message, stack })
  process.exit(1)
}
