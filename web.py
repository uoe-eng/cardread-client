from http.server import BaseHTTPRequestHandler, HTTPServer

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"GET request received, returning 200 OK.")

    def do_POST(self):
        content_len = int(self.headers.get('Content-Length'))
        print('POST', self.rfile.read(content_len))
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"POST request received, returning 200 OK.")

    def log_message(self, format, *args):
        return  # Suppress log output to the console

def run(server_class=HTTPServer, handler_class=SimpleHTTPRequestHandler, port=6543):
    server_address = ('', port)
    httpd = server_class(server_address, handler_class)
    print(f"Serving on port {port}")
    httpd.serve_forever()

if __name__ == "__main__":
    run()
