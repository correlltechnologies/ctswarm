import type { Express } from "express";
import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";

import { createApp } from "../src/server.js";
import { store } from "../src/store.js";

let app: Express;

beforeEach(() => {
  // Reset between cases so ordering cannot make a test pass or fail.
  store.reset();
  app = createApp();
});

describe("GET /items", () => {
  it("returns an empty list initially", async () => {
    const response = await request(app).get("/items").expect(200);
    expect(response.body).toEqual({ success: true, data: [], error: null });
  });

  it("returns created items", async () => {
    await request(app).post("/items").send({ name: "widget", quantity: 3 });
    const response = await request(app).get("/items").expect(200);
    expect(response.body.data).toHaveLength(1);
    expect(response.body.data[0]).toMatchObject({ name: "widget", quantity: 3 });
  });
});

describe("POST /items", () => {
  it("creates an item and returns 201", async () => {
    const response = await request(app)
      .post("/items")
      .send({ name: "widget", quantity: 5 })
      .expect(201);

    expect(response.body.success).toBe(true);
    expect(response.body.data).toMatchObject({ name: "widget", quantity: 5 });
    expect(response.body.data.id).toBeTruthy();
    expect(response.body.data.createdAt).toBeTruthy();
  });

  it("trims whitespace from the name", async () => {
    const response = await request(app)
      .post("/items")
      .send({ name: "  spaced  ", quantity: 1 })
      .expect(201);
    expect(response.body.data.name).toBe("spaced");
  });

  it.each([
    [{ quantity: 1 }, "name"],
    [{ name: "", quantity: 1 }, "name"],
    [{ name: "x" }, "quantity"],
    [{ name: "x", quantity: -1 }, "quantity"],
    [{ name: "x", quantity: 1.5 }, "quantity"],
  ])("rejects invalid input %j on field %s", async (body, field) => {
    const response = await request(app).post("/items").send(body).expect(400);
    expect(response.body.success).toBe(false);
    expect(response.body.field).toBe(field);
  });

  it("does not store an item when validation fails", async () => {
    await request(app).post("/items").send({ name: "", quantity: 1 }).expect(400);
    expect(store.size).toBe(0);
  });
});

describe("GET /items/:id", () => {
  it("returns a single item", async () => {
    const created = await request(app)
      .post("/items")
      .send({ name: "widget", quantity: 2 });
    const response = await request(app)
      .get(`/items/${created.body.data.id}`)
      .expect(200);
    expect(response.body.data.name).toBe("widget");
  });

  it("returns 404 for a missing item", async () => {
    const response = await request(app).get("/items/does-not-exist").expect(404);
    expect(response.body).toEqual({
      success: false,
      data: null,
      error: "item not found",
    });
  });
});

describe("DELETE /items/:id", () => {
  it("deletes an existing item", async () => {
    const created = await request(app)
      .post("/items")
      .send({ name: "widget", quantity: 1 });
    await request(app).delete(`/items/${created.body.data.id}`).expect(204);
    expect(store.size).toBe(0);
  });

  it("returns 404 when deleting a missing item", async () => {
    await request(app).delete("/items/nope").expect(404);
  });
});

describe("error envelope", () => {
  /**
   * Every response, including for unknown routes, uses the same envelope.
   *
   * This is load-bearing rather than cosmetic: a stray HTML 404 from Express's
   * default handler breaks any client that assumes JSON, and it is an easy thing
   * to regress when adding a route in the wrong position relative to the
   * catch-all.
   */
  it("returns JSON for unknown routes", async () => {
    const response = await request(app).get("/no-such-route").expect(404);
    expect(response.headers["content-type"]).toMatch(/application\/json/);
    expect(response.body).toEqual({
      success: false,
      data: null,
      error: "route not found",
    });
  });
});
