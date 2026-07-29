/**
 * In-memory item store.
 *
 * Deliberately not a database. A real datastore would add migration, connection,
 * and fixture failure modes that have nothing to do with whether the agent
 * factory works, and debugging the factory through an unrelated connection error
 * wastes the pilot.
 */

export interface Item {
  id: string;
  name: string;
  quantity: number;
  createdAt: string;
}

export interface CreateItemInput {
  name: string;
  quantity: number;
}

export class ValidationError extends Error {
  constructor(
    message: string,
    public readonly field: string,
  ) {
    super(message);
    this.name = "ValidationError";
  }
}

export class ItemStore {
  private items = new Map<string, Item>();
  private nextId = 1;

  /**
   * Validate at the boundary and fail with a field-specific message.
   *
   * Returning which field was wrong matters: a generic "invalid input" forces the
   * caller to guess, and an agent debugging against this API would guess wrong.
   */
  private validate(input: unknown): CreateItemInput {
    if (typeof input !== "object" || input === null) {
      throw new ValidationError("body must be an object", "body");
    }

    const { name, quantity } = input as Record<string, unknown>;

    if (typeof name !== "string" || name.trim().length === 0) {
      throw new ValidationError("name must be a non-empty string", "name");
    }
    if (name.length > 200) {
      throw new ValidationError("name must be 200 characters or fewer", "name");
    }
    if (typeof quantity !== "number" || !Number.isInteger(quantity)) {
      throw new ValidationError("quantity must be an integer", "quantity");
    }
    if (quantity < 0) {
      throw new ValidationError("quantity must not be negative", "quantity");
    }

    return { name: name.trim(), quantity };
  }

  create(input: unknown): Item {
    const { name, quantity } = this.validate(input);
    const item: Item = {
      id: String(this.nextId++),
      name,
      quantity,
      createdAt: new Date().toISOString(),
    };
    // Store a copy so a later mutation of the returned object cannot reach
    // through into stored state.
    this.items.set(item.id, { ...item });
    return item;
  }

  list(): Item[] {
    return [...this.items.values()].map((item) => ({ ...item }));
  }

  get(id: string): Item | undefined {
    const item = this.items.get(id);
    return item ? { ...item } : undefined;
  }

  delete(id: string): boolean {
    return this.items.delete(id);
  }

  /** Used by tests to guarantee isolation between cases. */
  reset(): void {
    this.items.clear();
    this.nextId = 1;
  }

  get size(): number {
    return this.items.size;
  }
}

export const store = new ItemStore();
