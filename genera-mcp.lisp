;;; server.lisp - MCP server that bridges to a Symbolics Genera Lisp machine via TCP.
;;; Uses JSON-RPC 2.0 over stdio (MCP stdio transport).
;;; Requires: SBCL, Quicklisp (for usocket, yason)

(require :asdf)
#-quicklisp
(let ((quicklisp-init (merge-pathnames "quicklisp/setup.lisp" (user-homedir-pathname))))
  (when (probe-file quicklisp-init)
    (load quicklisp-init)))

(ql:quickload '(:usocket :yason) :silent t)

(defpackage #:genera-mcp
  (:use #:cl)
  (:export #:main))

(in-package #:genera-mcp)

;;; --- JSON object builder ---

(defmacro jso (&rest pairs)
  "Build a hash table from alternating string-key / value pairs.
Example: (jso \"jsonrpc\" \"2.0\" \"id\" id \"result\" result)"
  (let ((ht (gensym "HT")))
    `(let ((,ht (make-hash-table :test 'equal)))
       ,@(loop for (k v) on pairs by #'cddr
               collect `(setf (gethash ,k ,ht) ,v))
       ,ht)))

;;; --- Configuration ---

(defvar *genera-host* (or (uiop:getenv "GENERA_HOST") "genera"))
(defvar *genera-port* (parse-integer (or (uiop:getenv "GENERA_PORT") "8888")))
(defvar *genera-timeout* (parse-integer (or (uiop:getenv "GENERA_TIMEOUT") "300")))

;;; --- Genera TCP client ---
;;; Protocol: length-prefixed fields.
;;; Request:  <length><space><expression bytes>
;;; Response: <newline><length><space><result>
;;;           <newline><length><space><stdout>
;;;           <newline><length><space><stderr>

(defun read-field (stream)
  "Read a length-prefixed field from Genera's response (binary stream).
Skips whitespace/newlines until a digit, reads the decimal length, then reads
that many bytes.  Non-ASCII octets are replaced with '?' (mirroring the Python
version's errors='replace' strategy)."
  (let ((buf (make-array 16 :element-type 'character :fill-pointer 0)))
    ;; Skip whitespace until first digit
    (loop for byte = (read-byte stream)
          for ch = (code-char byte)
          when (digit-char-p ch)
            do (vector-push-extend ch buf)
               (return))
    ;; Read remaining digits until space
    (loop for byte = (read-byte stream)
          for ch = (code-char byte)
          while (digit-char-p ch)
          do (vector-push-extend ch buf))
    (let* ((length (parse-integer (coerce buf 'string)))
           (data (make-array length :element-type '(unsigned-byte 8))))
      (when (plusp length)
        (read-sequence data stream))
      ;; Convert bytes to string, replacing non-ASCII with '?', normalizing line endings
      (with-output-to-string (out)
        (loop with i = 0
              while (< i length)
              for byte = (aref data i)
              for ch = (if (< byte 128) (code-char byte) #\?)
              do (cond ((and (char= ch #\Return)
                             (< (1+ i) length)
                             (< (aref data (1+ i)) 128)
                             (char= (code-char (aref data (1+ i))) #\Linefeed))
                        (write-char #\Newline out)
                        (incf i 2))
                       ((char= ch #\Return)
                        (write-char #\Newline out)
                        (incf i))
                       (t
                        (write-char ch out)
                        (incf i))))))))

(defun string-to-octets (string)
  "Convert an ASCII string to a byte vector."
  (let ((octets (make-array (length string) :element-type '(unsigned-byte 8))))
    (loop for i below (length string)
          do (setf (aref octets i) (char-code (char string i))))
    octets))

(defun genera-send (expression &key (host *genera-host*) (port *genera-port*))
  "Send a Lisp expression to Genera and return (values result stdout stderr)."
  (let ((socket (usocket:socket-connect host port :element-type '(unsigned-byte 8))))
    (unwind-protect
        (let* ((stream (usocket:socket-stream socket))
               (expr-bytes (string-to-octets expression))
               (header (string-to-octets (format nil "~D " (length expr-bytes)))))
          ;; Send: length + space + expression
          (write-sequence header stream)
          (write-sequence expr-bytes stream)
          (force-output stream)
          ;; Read three response fields
          (values (read-field stream)
                  (read-field stream)
                  (read-field stream)))
      (usocket:socket-close socket))))

;;; --- Response formatting ---

(defun format-response (result stdout stderr)
  "Format the three response fields into a single string for the LLM."
  (let ((parts '()))
    (when (plusp (length stderr))
      (push (format nil "[stderr]~%~A" stderr) parts))
    (when (plusp (length stdout))
      (push (format nil "[stdout]~%~A" stdout) parts))
    (when (plusp (length result))
      (push result parts))
    (if parts
        (format nil "~{~A~^~%~%~}" parts)
        "(no output)")))

;;; --- MCP JSON-RPC over stdio ---

(defun log-message (fmt &rest args)
  "Write a log message to stderr (MCP uses stdout for protocol)."
  (apply #'format *error-output* fmt args)
  (terpri *error-output*)
  (force-output *error-output*))

(defun read-json-rpc ()
  "Read one JSON-RPC message from stdin. Returns NIL on EOF."
  (let ((line (read-line *standard-input* nil nil)))
    (when (and line (plusp (length line)))
      (yason:parse line))))

(defun write-json-rpc (response)
  "Write a JSON-RPC response as a single line to stdout."
  (yason:encode response *standard-output*)
  (terpri *standard-output*)
  (force-output *standard-output*))

(defun make-response (id result)
  "Build a JSON-RPC 2.0 success response."
  (jso "jsonrpc" "2.0" "id" id "result" result))

(defun make-error-response (id code message)
  "Build a JSON-RPC 2.0 error response."
  (jso "jsonrpc" "2.0"
       "id" id
       "error" (jso "code" code "message" message)))

;;; --- MCP protocol handlers ---

(defun handle-initialize (id)
  (make-response id
    (jso "protocolVersion" "2024-11-05"
         "capabilities" (jso "tools" (jso "listChanged" 'yason:false))
         "serverInfo"   (jso "name" "genera" "version" "1.0.0"))))

(defun handle-tools-list (id)
  (make-response id
    (jso "tools"
         (list (jso "name" "eval_lisp"
                    "description" "Evaluate a Lisp expression on a Symbolics Genera machine and return the result."
                    "inputSchema" (jso "type" "object"
                                       "properties" (jso "expression"
                                                         (jso "type" "string"
                                                              "description" "A Common Lisp / Zetalisp expression to evaluate."))
                                       "required" '("expression")))))))

(defun handle-tools-call (id params)
  (let* ((name (gethash "name" params))
         (arguments (gethash "arguments" params)))
    (if (string= name "eval_lisp")
        (let ((text (handler-case
                        (sb-ext:with-timeout *genera-timeout*
                          (multiple-value-bind (result stdout stderr)
                              (genera-send (gethash "expression" arguments))
                            (format-response result stdout stderr)))
                      (sb-ext:timeout ()
                        (format nil "Timeout: Genera did not respond within ~Ds" *genera-timeout*))
                      (error (e)
                        (format nil "Connection error: ~A" e)))))
          (make-response id
                         (jso "content" (list (jso "type" "text" "text" text)))))
        (make-error-response id -32602 (format nil "Unknown tool: ~A" name)))))

;;; --- Main loop ---

(defun handle-request (msg)
  "Dispatch a JSON-RPC request and return a response (or NIL for notifications)."
  (let ((method (gethash "method" msg))
        (id (gethash "id" msg))
        (params (gethash "params" msg)))
    (cond
      ((string= method "initialize")
       (handle-initialize id))
      ((string= method "notifications/initialized")
       nil)
      ((string= method "tools/list")
       (handle-tools-list id))
      ((string= method "tools/call")
       (handle-tools-call id params))
      ((string= method "ping")
       (make-response id (jso)))
      (id
       (make-error-response id -32601 (format nil "Method not found: ~A" method)))
      (t nil))))

(defun main ()
  "MCP stdio server main loop."
  (log-message "genera MCP server starting (CL/SBCL)")
  (loop for msg = (read-json-rpc)
        while msg
        do (let ((response (handle-request msg)))
             (when response
               (write-json-rpc response)))))

;;; Run via: sbcl --noinform --non-interactive --load server.lisp --eval '(genera-mcp:main)'
