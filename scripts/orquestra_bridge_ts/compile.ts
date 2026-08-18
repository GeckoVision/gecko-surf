/**
 * Entry point: run Orquestra's own `compile()` over one FDL document.
 *
 * stdin  {"doc": <FDL document>, "skipNodeTypeCheck": <bool>}
 * stdout one `__ORQUESTRA_BRIDGE_RESULT__` line carrying his CompileResult verbatim
 *        — `{ok: true, plan}` or `{ok: false, errors: [{nodeId?, path?, message}]}`.
 *
 * His errors are passed through untouched. They are the product of this bridge:
 * a projector that emits a document his compiler rejects has to hear his words,
 * not ours.
 */

import { loadEngine, emit, readRequest, fail } from './engine.ts'

interface CompileRequest {
  doc: unknown
  skipNodeTypeCheck?: boolean
}

try {
  const { compile } = await loadEngine()
  const request = await readRequest<CompileRequest>()
  emit(await compile(request.doc, { skipNodeTypeCheck: request.skipNodeTypeCheck === true }))
} catch (error) {
  fail(error)
}
