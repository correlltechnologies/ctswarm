/**
 * The route table.
 *
 * Exported as data rather than being registered inline so that the contract test
 * can enumerate what the service actually serves without parsing source or
 * poking at Express internals. Keeping the table declarative is also what makes
 * it obvious, on review, when a route was added but its documentation was not.
 */

export type HttpMethod = "get" | "post" | "put" | "patch" | "delete";

export interface RouteDefinition {
  method: HttpMethod;
  /** Express-style path, e.g. "/items/:id". */
  path: string;
  /** Short description mirrored into the OpenAPI summary. */
  summary: string;
}

export const ROUTES: readonly RouteDefinition[] = [
  { method: "get", path: "/items", summary: "List all items" },
  { method: "post", path: "/items", summary: "Create an item" },
  { method: "get", path: "/items/:id", summary: "Fetch a single item" },
  { method: "delete", path: "/items/:id", summary: "Delete an item" },
] as const;

/**
 * Convert an Express path to its OpenAPI equivalent.
 *
 * Express uses ":id" and OpenAPI uses "{id}". The contract test compares the two
 * documents, so the translation has to live in exactly one place or the two
 * representations will drift in ways the test then reports as a false failure.
 */
export function toOpenApiPath(expressPath: string): string {
  return expressPath.replace(/:([A-Za-z0-9_]+)/g, "{$1}");
}
