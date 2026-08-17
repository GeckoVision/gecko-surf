/**
 * A light fake for the Cloudflare bindings Orquestra's flow engine expects.
 *
 * `NodeContext` is `{ db, cache, idls, rpcUrl }` (flow-engine/types.ts). Only
 * `rpcUrl` is real here: the flow we run offline reaches `db`/`cache`/`idls`
 * through exactly one path — `fetchProjectIdl(projectId, { DB, IDLS })`, which
 * reads `IDLS.get("project:<id>")` first and falls back to a D1 SELECT.
 *
 * So the fake serves ONE key shape, `project:<id>`, for project ids it was
 * explicitly given, and EVERY other access throws `FakeContextError`. That is
 * the whole point: a fake that quietly returns `null` for an unknown project id
 * would make `fetchProjectIdl` return null, and the failure would surface as
 * "project not found" — indistinguishable from a real registry miss. A fake
 * that quietly returned an IDL for any id would let a flow that names the wrong
 * project pass. Both are silent wrong answers; both are unreachable here.
 *
 * Nothing in this file signs or broadcasts. `db`/`cache` are pure tripwires.
 */

export class FakeContextError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'FakeContextError'
  }
}

/** The row shape `fetchProjectIdl` reads out of the IDLS KV namespace. */
export interface ProjectIdlRow {
  idl: unknown
  programId: string
  projectName: string
}

/** A binding whose every property access is a tripwire, naming what it was asked for. */
function tripwire(surface: string, why: string): any {
  return new Proxy(
    {},
    {
      get(_target, property) {
        throw new FakeContextError(
          `ctx.${surface}.${String(property)} was read, but this fake serves no ${surface}: ${why}`,
        )
      },
    },
  )
}

/**
 * The IDLS KV namespace. `get` answers only for the registered project ids;
 * every other key, and every other KV method (`put`, `list`, `delete`,
 * `getWithMetadata`, …), throws.
 */
export function fakeIdls(projects: Record<string, ProjectIdlRow>): any {
  const served = new Map<string, ProjectIdlRow>()
  for (const [projectId, row] of Object.entries(projects)) {
    served.set(`project:${projectId}`, row)
  }

  const impl = {
    async get(key: string): Promise<string> {
      const row = served.get(key)
      if (!row) {
        throw new FakeContextError(
          `ctx.idls.get(${JSON.stringify(key)}) — this fake was built to serve only ` +
            `[${[...served.keys()].map((k) => JSON.stringify(k)).join(', ')}]. ` +
            'Register that project id with the bridge, or fix the projectId the flow passes.',
        )
      }
      return JSON.stringify(row)
    },
  }

  return new Proxy(impl, {
    get(target, property) {
      if (property === 'get') return target.get
      throw new FakeContextError(
        `ctx.idls.${String(property)}() was called; this fake implements only get("project:<id>")`,
      )
    },
  })
}

/**
 * Build the `NodeContext` handed to every node's `run()`.
 *
 * `db` is a tripwire on purpose: reaching D1 means either `resolve.constant@1`
 * (a protocol-descriptor lookup this bridge has no table for) or the IDL
 * registry's D1 fallback — and the fallback is only reachable if the KV read
 * above already refused, which throws first.
 */
export function fakeContext(rpcUrl: string, projects: Record<string, ProjectIdlRow>): any {
  return {
    db: tripwire(
      'db',
      'the only D1 readers are resolve.constant@1 (protocol_descriptors) and the IDL registry fallback',
    ),
    cache: tripwire('cache', 'no registered node reads ctx.cache today'),
    idls: fakeIdls(projects),
    rpcUrl,
  }
}
