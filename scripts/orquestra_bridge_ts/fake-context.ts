/**
 * A light fake for the Cloudflare bindings Orquestra's flow engine expects.
 *
 * `NodeContext` is `{ db, cache, idls, rpcUrl }` (flow-engine/types.ts). Only
 * `rpcUrl` is real here: the flow we run offline reaches `db`/`cache`/`idls`
 * through exactly one path — `fetchProjectIdl(projectId, { DB, IDLS })`.
 *
 * That path grew a step upstream (`fix(security): … enforce IDL visibility`).
 * It now reads D1 FIRST, through `getVisibleProject`, because checking
 * visibility only on the D1 fallback served a cached private IDL to anyone. So
 * the order is: `projects` row, then `IDLS.get("project:<id>")`, then the D1
 * IDL fallback. The fake models the first two and refuses the third.
 *
 * Both served surfaces answer only for project ids the bridge was explicitly
 * given, and EVERY other access throws `FakeContextError`. That is the whole
 * point: a fake that quietly returned `null` for an unknown project id would
 * make `fetchProjectIdl` return null, and the failure would surface as "project
 * not found" — indistinguishable from a real registry miss. A fake that quietly
 * returned an IDL for any id would let a flow that names the wrong project pass.
 * Both are silent wrong answers; both are unreachable here.
 *
 * Note this means an unknown project id is now refused at the VISIBILITY read,
 * before the KV registry is ever consulted — the earlier surface, not a weaker
 * one.
 *
 * Nothing in this file signs or broadcasts. `cache` is a pure tripwire.
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

/** The one D1 statement `getVisibleProject` issues, matched exactly. */
const VISIBILITY_SQL = 'SELECT id, program_id, is_public, user_id FROM projects WHERE id = ?'

/**
 * The D1 binding. It answers exactly one statement — the project-visibility
 * SELECT that now runs ahead of every IDL read — and only for registered ids.
 *
 * An unregistered id must NOT come back as `null` here: `getVisibleProject`
 * renders a miss as "not visible", `fetchProjectIdl` turns that into `null`,
 * and the node reports an ordinary "project not found". That is precisely the
 * silent wrong answer this fake exists to make unreachable, so it throws by
 * name instead. Every other statement — `resolve.constant@1`'s
 * protocol_descriptors lookup, the IDL registry's D1 fallback — is a tripwire.
 */
export function fakeDb(projects: Record<string, ProjectIdlRow>): any {
  const known = new Set(Object.keys(projects))

  const impl = {
    prepare(sql: string) {
      if (sql.replace(/\s+/g, ' ').trim() !== VISIBILITY_SQL) {
        throw new FakeContextError(
          `ctx.db.prepare(${JSON.stringify(sql)}) — this fake serves only the project ` +
            'visibility read; the other D1 readers (resolve.constant@1 protocol_descriptors, ' +
            'the IDL registry D1 fallback) have no table here.',
        )
      }
      return {
        bind(projectId: string) {
          return {
            async first(): Promise<unknown> {
              if (!known.has(projectId)) {
                throw new FakeContextError(
                  `ctx.db … FROM projects WHERE id = ${JSON.stringify(projectId)} — this ` +
                    `fake was built to serve only [${[...known]
                      .map((id) => JSON.stringify(id))
                      .join(', ')}]. Register that project id with the bridge, or fix the ` +
                    'projectId the flow passes.',
                )
              }
              return {
                id: projectId,
                program_id: projects[projectId].programId,
                is_public: 1,
                user_id: null,
              }
            },
          }
        },
      }
    },
  }

  return new Proxy(impl, {
    get(target, property) {
      if (property === 'prepare') return target.prepare
      throw new FakeContextError(
        `ctx.db.${String(property)} was read; this fake implements only prepare(<visibility SELECT>)`,
      )
    },
  })
}

/** Build the `NodeContext` handed to every node's `run()`. */
export function fakeContext(rpcUrl: string, projects: Record<string, ProjectIdlRow>): any {
  return {
    db: fakeDb(projects),
    cache: tripwire('cache', 'no registered node reads ctx.cache today'),
    idls: fakeIdls(projects),
    rpcUrl,
  }
}
