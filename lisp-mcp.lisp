;;; server.lisp - MCP server that bridges to a Symbolics Genera Lisp machine via TCP.
;;; Uses JSON-RPC 2.0 over stdio (MCP stdio transport).
;;; Requires: SBCL, Quicklisp (for usocket, yason)

(require :asdf)
#-quicklisp
(let ((quicklisp-init (merge-pathnames "quicklisp/setup.lisp" (user-homedir-pathname))))
  (when (probe-file quicklisp-init)
    (load quicklisp-init)))

(ql:quickload '(:usocket :yason) :silent t)

(defpackage #:lisp-mcp
  (:use #:cl)
  (:export #:main))

(in-package #:lisp-mcp)

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

(defvar *genera-host* (uiop:getenv "GENERA_HOST"))
(defvar *genera-port* (parse-integer (or (uiop:getenv "GENERA_PORT") "8888")))
(defvar *genera-timeout* (parse-integer (or (uiop:getenv "GENERA_TIMEOUT") "300")))
(defvar *genera-enabled* (not (null *genera-host*)))

(defvar *swank-host* (or (uiop:getenv "SWANK_HOST") "127.0.0.1"))
(defvar *swank-port* (parse-integer (or (uiop:getenv "SWANK_PORT") "4005")))
(defvar *swank-package* (or (uiop:getenv "SWANK_PACKAGE") "COMMON-LISP-USER"))
(defvar *swank-enabled* (not (null (uiop:getenv "SWANK_PORT"))))

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

(defun strip-lisp-comments (string)
  "Remove ;-style line comments from STRING, preserving strings and #|...|# blocks.
Prevents comment-induced EOF errors on the Genera reader."
  (with-output-to-string (out)
    (loop with len = (length string)
          with i = 0
          while (< i len)
          for ch = (char string i)
          do (cond
               ;; String literal — copy verbatim through closing quote
               ((char= ch #\")
                (write-char ch out)
                (incf i)
                (loop while (< i len)
                      for c = (char string i)
                      do (write-char c out)
                         (incf i)
                      when (char= c #\\)
                        do (when (< i len)
                             (write-char (char string i) out)
                             (incf i))
                      when (char= c #\")
                        do (return)))
               ;; Line comment — skip to end of line
               ((char= ch #\;)
                (loop while (and (< i len)
                                 (char/= (char string i) #\Newline))
                      do (incf i)))
               ;; Everything else — copy
               (t (write-char ch out)
                  (incf i))))))

(defun genera-send (expression &key (host *genera-host*) (port *genera-port*))
  "Send a Lisp expression to Genera and return (values result stdout stderr)."
  (let ((socket (usocket:socket-connect host port :element-type '(unsigned-byte 8))))
    (unwind-protect
        (let* ((stream (usocket:socket-stream socket))
               (clean-expr (strip-lisp-comments expression))
               (expr-bytes (string-to-octets clean-expr))
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

;;; --- Swank client ---
;;; Protocol: length-prefixed S-expressions over TCP.
;;; Each message is a 6-char hex length followed by the S-expression payload.
;;; We send :emacs-rex with swank:eval-and-grab-output, then read messages
;;; until we get a :return reply.

(defun swank-read-message (stream)
  "Read one Swank wire message: 6-char hex length then payload. Returns the parsed sexp."
  (let ((header (make-string 6)))
    (read-sequence header stream)
    (let* ((len (parse-integer header :radix 16))
           (payload (make-string len)))
      (read-sequence payload stream)
      (read-from-string payload))))

(defvar *swank-counter* 0)

(defun swank-send-raw (stream fmt &rest args)
  "Format a Swank wire message and write it to STREAM."
  (let* ((payload (apply #'format nil fmt args))
         (header (format nil "~6,'0X" (length payload))))
    (write-string header stream)
    (write-string payload stream)
    (force-output stream)))

(defun swank-eval (expression &key (host *swank-host*) (port *swank-port*)
                                   (package *swank-package*))
  "Send EXPRESSION to a Swank server for evaluation.
Returns (values result-string output-string)."
  (let* ((socket (usocket:socket-connect host port :element-type 'character))
         (stream (usocket:socket-stream socket))
         (id (incf *swank-counter*)))
    (unwind-protect
        (progn
          ;; Send eval request — format as raw string to avoid needing
          ;; the SWANK package in the MCP server's image.
          (swank-send-raw stream
            "(:emacs-rex (swank:eval-and-grab-output ~S) ~S t ~D)"
            expression package id)
          ;; Read messages until :return for our request.
          ;; swank:eval-and-grab-output returns (output result).
          ;; On error, Swank enters the debugger — we extract the error
          ;; message from :debug, abort, and wait for the :return.
          (let ((output-parts '())
                (error-message nil))
            (loop
              (let ((msg (swank-read-message stream)))
                (case (first msg)
                  (:return
                   (when (eql (third msg) id)
                     (let ((value (second msg)))
                       (case (first value)
                         (:ok
                          (let ((stdout (first (second value)))
                                (result (second (second value))))
                            (return (values result
                                            (apply #'concatenate 'string
                                                   (nreverse (cons stdout output-parts)))))))
                         (:abort
                          (return (values "" (or error-message
                                                 (format nil "Aborted: ~A" (second value))))))
                         (otherwise
                          (return (values "" (format nil "Unexpected return: ~S" value))))))))
                  (:debug
                   ;; (:debug thread level (desc ...) restarts ...)
                   ;; Extract the error description (first element of 4th item).
                   (setf error-message (first (fourth msg))))
                  (:debug-activate
                   ;; Abort the debugger: find the ABORT restart and invoke it.
                   ;; Restarts are in the 5th element of the preceding :debug msg.
                   ;; We look for one named "*ABORT" or "ABORT"; fall back to index 1.
                   (let* ((thread (second msg))
                          (level (third msg))
                          (abort-id (incf *swank-counter*)))
                     (swank-send-raw stream
                       "(:emacs-rex (swank:invoke-nth-restart-for-emacs ~D 1) ~S ~D ~D)"
                       level package thread abort-id)))
                  (:write-string
                   (push (second msg) output-parts))
                  (otherwise nil))))))
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

;;; --- JSON-RPC 2.0 error codes ---

(defconstant +parse-error+      -32700)
(defconstant +invalid-request+  -32600)
(defconstant +method-not-found+ -32601)
(defconstant +invalid-params+   -32602)
(defconstant +internal-error+   -32603)

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
         "serverInfo"   (jso "name" "lisp-mcp" "version" "1.0.0"))))

(defun make-genera-tool ()
  (jso "name" "eval_genera"
       "description" "Evaluate a Lisp expression on a Symbolics Genera machine and return the result."
       "inputSchema" (jso "type" "object"
                          "properties" (jso "expression"
                                            (jso "type" "string"
                                                 "description" "Evaluate a Lisp expression on a Symbolics Genera machine and return the result."))
                          "required" '("expression"))))

(defun make-host-cl-tool ()
  (jso "name" "eval_host_cl"
       "description" "Evaluate a Common Lisp expression in the host Common Lisp process and return the result."
       "inputSchema" (jso "type" "object"
                          "properties" (jso "expression"
                                            (jso "type" "string"
                                                 "description" "A Common Lisp expression to evaluate locally in the host Common Lisp."))
                          "required" '("expression"))))

(defun make-swank-tool ()
  (jso "name" "eval_swank"
       "description" "Evaluate a Common Lisp expression on a running Lisp with a Swank server and return the result."
       "inputSchema" (jso "type" "object"
                          "properties" (jso "expression"
                                            (jso "type" "string"
                                                 "description" "A Common Lisp expression to evaluate on the Swank-connected Lisp."))
                          "required" '("expression"))))

(defun handle-tools-list (id)
  (let ((tools (list (make-host-cl-tool))))
    (when *swank-enabled*
      (push (make-swank-tool) tools))
    (when *genera-enabled*
      (push (make-genera-tool) tools))
    (make-response id (jso "tools" tools))))

(defun eval-genera (expression)
  "Evaluate EXPRESSION on the remote Genera machine. Returns a formatted string."
  (handler-case
      (sb-ext:with-timeout *genera-timeout*
        (multiple-value-bind (result stdout stderr)
            (genera-send expression)
          (format-response result stdout stderr)))
    (sb-ext:timeout ()
      (format nil "Timeout: Genera did not respond within ~Ds" *genera-timeout*))
    (error (e)
      (format nil "Connection error: ~A" e))))

(defun eval-swank-tool (expression)
  "Evaluate EXPRESSION via a remote Swank server. Returns a formatted string."
  (handler-case
      (sb-ext:with-timeout *genera-timeout*
        (multiple-value-bind (result output)
            (swank-eval expression)
          (format-response result output "")))
    (sb-ext:timeout ()
      (format nil "Timeout: Swank did not respond within ~Ds" *genera-timeout*))
    (error (e)
      (format nil "Connection error: ~A" e))))

(defun eval-host-cl (expression)
  "Evaluate EXPRESSION locally in the host Common Lisp. Returns a formatted string."
  (handler-case
      (sb-ext:with-timeout *genera-timeout*
        (let* ((*standard-output* (make-string-output-stream))
               (*error-output*    (make-string-output-stream))
               (*trace-output*    (make-string-output-stream))
               (result (format nil "~{~S~^~%~}"
                               (multiple-value-list
                                (eval (read-from-string expression))))))
          (format-response result
                           (get-output-stream-string *standard-output*)
                           (get-output-stream-string *error-output*))))
    (sb-ext:timeout ()
      (format nil "Timeout: evaluation did not complete within ~Ds" *genera-timeout*))
    (error (e)
      (format nil "Error: ~A" e))))

(defun handle-tools-call (id params)
  (let* ((name (gethash "name" params))
         (arguments (gethash "arguments" params))
         (expression (gethash "expression" arguments)))
    (let ((text (cond
                  ((string= name "eval_genera") (eval-genera expression))
                  ((string= name "eval_host_cl") (eval-host-cl expression))
                  ((string= name "eval_swank")   (eval-swank-tool expression))
                  (t (return-from handle-tools-call
                       (make-error-response id +invalid-params+
                                            (format nil "Unknown tool: ~A" name)))))))
      (make-response id
                     (jso "content" (list (jso "type" "text" "text" text)))))))

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
       (make-error-response id +method-not-found+ (format nil "Method not found: ~A" method)))
      (t nil))))

(defun main ()
  "MCP stdio server main loop."
  (log-message "lisp-mcp server starting (SBCL~@[, Genera at ~A:~D~]~@[, Swank at ~A:~D~])"
                (when *genera-enabled* *genera-host*)
                (when *genera-enabled* *genera-port*)
                (when *swank-enabled* *swank-host*)
                (when *swank-enabled* *swank-port*))
  (loop for msg = (read-json-rpc)
        while msg
        do (let ((response (handle-request msg)))
             (when response
               (write-json-rpc response)))))

;;; Run via: sbcl --noinform --non-interactive --load lisp-mcp.lisp --eval '(lisp-mcp:main)'
