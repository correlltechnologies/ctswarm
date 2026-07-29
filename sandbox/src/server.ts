import express, { type Express, type NextFunction, type Request, type Response } from "express";

import { ROUTES } from "./routes.js";
import { ValidationError, store } from "./store.js";

export const SERVICE_VERSION = "1.0.0";

/**
 * Process start time, captured at module load.
 *
 * Kept here rather than inside a handler so uptime is measured from process
 * start rather than from first request.
 */
export const STARTED_AT = Date.now();

export function createApp(): Express {
  const app = express();
  app.use(express.json({ limit: "64kb" }));

  app.get("/items", (_req: Request, res: Response) => {
    res.json({ success: true, data: store.list(), error: null });
  });

  app.post("/items", (req: Request, res: Response, next: NextFunction) => {
    try {
      const item = store.create(req.body);
      res.status(201).json({ success: true, data: item, error: null });
    } catch (error) {
      next(error);
    }
  });

  app.get("/items/:id", (req: Request, res: Response) => {
    const item = store.get(req.params.id);
    if (!item) {
      res.status(404).json({ success: false, data: null, error: "item not found" });
      return;
    }
    res.json({ success: true, data: item, error: null });
  });

  app.delete("/items/:id", (req: Request, res: Response) => {
    const deleted = store.delete(req.params.id);
    if (!deleted) {
      res.status(404).json({ success: false, data: null, error: "item not found" });
      return;
    }
    res.status(204).end();
  });

  // Unknown routes answer in the same envelope as everything else. A stray HTML
  // 404 from the default handler would break any client that assumes JSON.
  app.use((_req: Request, res: Response) => {
    res.status(404).json({ success: false, data: null, error: "route not found" });
  });

  app.use(
    (error: Error, _req: Request, res: Response, _next: NextFunction) => {
      if (error instanceof ValidationError) {
        res.status(400).json({
          success: false,
          data: null,
          error: error.message,
          field: error.field,
        });
        return;
      }
      // Log detail server-side, return something safe. Leaking internals through
      // an error body is how stack traces end up in client logs.
      console.error("unhandled error:", error);
      res.status(500).json({ success: false, data: null, error: "internal error" });
    },
  );

  return app;
}

/** Route table accessor, used by the contract test. */
export function registeredRoutes() {
  return ROUTES;
}

// Only listen when executed directly, so importing the app in tests does not
// bind a port.
if (process.argv[1] && process.argv[1].endsWith("server.js")) {
  const port = Number(process.env.PORT ?? 3000);
  createApp().listen(port, () => {
    console.log(`sandbox listening on :${port}`);
  });
}
