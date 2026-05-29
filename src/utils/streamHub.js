export class StreamHub {
  constructor() {
    this.clients = new Set();
  }

  add(response, initialPayload) {
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      "connection": "keep-alive",
      "access-control-allow-origin": "*"
    });
    response.write(`event: snapshot\ndata: ${JSON.stringify(initialPayload)}\n\n`);
    this.clients.add(response);
    response.on("close", () => this.clients.delete(response));
  }

  broadcast(event, payload) {
    const message = `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
    for (const client of this.clients) {
      client.write(message);
    }
  }
}
