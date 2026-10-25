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

(load (merge-pathnames "genera-protocol.lisp" *load-truename*))

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

(defvar *profile* "generic")
(defvar *genera-protocol* "legacy")
(defvar *genera-host* nil)
(defvar *genera-port* 8888)
(defvar *genera-timeout* 300)
(defvar *genera-enabled* nil)
(defvar *genera-token* nil)
(defvar *genera-expected-world* nil)

(defvar *swank-host* "127.0.0.1")
(defvar *swank-port* 4005)
(defvar *swank-package* "COMMON-LISP-USER")
(defvar *swank-enabled* nil)

(define-condition configuration-error (simple-error) ())

(defun configuration-error (name requirement)
  (error 'configuration-error
         :format-control "Invalid configuration: ~A ~A."
         :format-arguments (list name requirement)))

(defun configuration-text (name &optional default)
  (let ((value (uiop:getenv name)))
    (when (and value
               (or (zerop (length value))
                   (not (string= value (string-trim '(#\Space) value)))
                   (some (lambda (ch)
                           (or (< (char-code ch) 32) (= (char-code ch) 127)))
                         value)))
      (configuration-error name
                           "must be nonempty text without surrounding whitespace or control characters; unset it if unused"))
    (or value default)))

(defun configuration-host (name &optional default)
  (let ((value (configuration-text name default)))
    (when (and value (find #\Space value))
      (configuration-error name "must be a hostname or IP address without spaces"))
    value))

(defun configuration-integer (name default minimum &optional maximum)
  (let ((value (uiop:getenv name)))
    (if (null value)
        default
        (let ((number (and (plusp (length value))
                           (every (lambda (ch) (char<= #\0 ch #\9)) value)
                           (parse-integer value))))
          (unless (and number (<= minimum number)
                       (or (null maximum) (<= number maximum)))
            (configuration-error
             name (if maximum
                      (format nil "must be a decimal integer from ~D to ~D"
                              minimum maximum)
                      (format nil "must be a decimal integer of at least ~D seconds"
                              minimum))))
          number))))

(defun configuration-choice (name default supported)
  (let ((value (configuration-text name default)))
    (unless (member value supported :test #'string=)
      (configuration-error name
                           (format nil "supports only ~{~S~^, ~} in this build"
                                   supported)))
    value))

(defvar *genera-runtime* nil)
(defparameter *genera-launch-source* (merge-pathnames "genera-launch.lisp" *load-truename*))

(defun genera-load-launch-module ()
  (unless (fboundp 'genera-launch-preflight) (load *genera-launch-source*)))

(defun genera-configured-runtime (profile protocol host port)
  ;; Keep optional POSIX inspection out of the ordinary attach-only load path.
  (when (or (fboundp 'genera-runtime-configuration)
            (some #'uiop:getenv
                  '("GENERA_RUNTIME_MODE" "GENERA_AUTOSTART" "GENERA_VLM_ROOT"
                    "GENERA_VLM_EXECUTABLE" "GENERA_VLM_WORLD" "GENERA_VLM_PARTITION"
                    "GENERA_VLM_DEBUGGER" "GENERA_VLM_USER" "GENERA_VLM_NETWORK"
                    "GENERA_STATE_DIR" "GENERA_VLM_LOG")))
    (genera-load-launch-module)
    (funcall (symbol-function 'genera-runtime-configuration) profile protocol host port)))

(defun genera-preflight (&optional runtime)
  "Inspect local launch prerequisites on demand, without starting a VLM."
  (genera-load-launch-module)
  (funcall (symbol-function 'genera-launch-preflight)
           (or runtime *genera-runtime*
               (funcall (symbol-function 'genera-runtime-configuration)
                        *profile* *genera-protocol* *genera-host* *genera-port*))))

(defun configure ()
  "Validate all consumed settings before entering the MCP loop or doing I/O."
  (let ((profile (configuration-choice "LISP_MCP_PROFILE" "generic" '("generic" "genera")))
        (protocol (configuration-choice "GENERA_PROTOCOL" "legacy" '("legacy" "v1")))
        (genera-host (configuration-host "GENERA_HOST"))
        (genera-port (configuration-integer "GENERA_PORT" 8888 1 65535))
        (timeout (configuration-integer "GENERA_TIMEOUT" 300 1))
        (swank-host (configuration-host "SWANK_HOST" "127.0.0.1"))
        (swank-port (configuration-integer "SWANK_PORT" 4005 1 65535))
        (swank-package (configuration-text "SWANK_PACKAGE" "COMMON-LISP-USER"))
        (token (configuration-text "GENERA_TOKEN"))
        (world (configuration-text "GENERA_EXPECTED_WORLD")))
    (when (and (string= profile "genera") (not (string= protocol "v1")))
      (configuration-error "LISP_MCP_PROFILE" "genera requires explicit GENERA_PROTOCOL=v1"))
    (when (and (string= protocol "v1") (not (string= profile "genera")))
      (configuration-error "GENERA_PROTOCOL" "v1 requires LISP_MCP_PROFILE=genera"))
    (when (and (string= profile "genera") (null genera-host))
      (configuration-error "GENERA_HOST" "is required by the genera profile"))
    (dolist (setting (list (cons "GENERA_TOKEN" token) (cons "GENERA_EXPECTED_WORLD" world)))
      (when (and (cdr setting)
                 (or (> (length (cdr setting)) 4096)
                     (some (lambda (ch) (> (char-code ch) 126)) (cdr setting))))
        (configuration-error (car setting) "must contain at most 4096 printable ASCII characters")))
    (when (and (or token world) (not (string= profile "genera")))
      (configuration-error (if token "GENERA_TOKEN" "GENERA_EXPECTED_WORLD")
                           "requires the genera profile and protocol v1"))
    (setf *genera-runtime* (genera-configured-runtime profile protocol genera-host genera-port)
          *profile* profile
          *genera-protocol* protocol
          *genera-host* genera-host
          *genera-port* genera-port
          *genera-timeout* timeout
          *genera-enabled* (not (null genera-host))
          *genera-token* token *genera-expected-world* world
          *swank-host* swank-host
          *swank-port* swank-port
          *swank-package* swank-package
          *swank-enabled* (not (null (uiop:getenv "SWANK_PORT"))))))

;;; --- Genera TCP client ---
;;; Protocol: length-prefixed fields.
;;; Request:  <length><space><expression bytes>
;;; Response: <newline><length><space><result>
;;;           <newline><length><space><stdout>
;;;           <newline><length><space><stderr>

(defconstant +max-header-digits+ 10)
(defconstant +max-frame-length+ 1048576)

(define-condition framing-error (simple-error) ())

(defun framing-error (control &rest arguments)
  (error 'framing-error :format-control control :format-arguments arguments))

(defun read-exactly (stream sequence)
  "Fill SEQUENCE, continuing short reads and rejecting premature EOF."
  (loop with start = 0
        while (< start (length sequence))
        do (let ((end (read-sequence sequence stream :start start)))
             (when (= start end)
               (framing-error "Truncated frame: expected ~D elements, received ~D"
                              (length sequence) start))
             (setf start end)))
  sequence)

(defun read-frame-length (stream)
  "Read decimal digits and one space, with at most one leading line break."
  (let ((byte (read-byte stream nil nil))
        (length 0)
        (digits 0))
    (cond ((member byte '(10 141)) (setf byte (read-byte stream nil nil)))
          ((eql byte 13)
           (setf byte (read-byte stream nil nil))
           (when (eql byte 10) (setf byte (read-byte stream nil nil)))))
    (loop
      (cond ((null byte) (framing-error "Truncated length header"))
            ((<= 48 byte 57)
             (when (> (incf digits) +max-header-digits+)
               (framing-error "Length header exceeds ~D digits" +max-header-digits+))
             (setf length (+ (* length 10) (- byte 48)))
             (when (> length +max-frame-length+)
               (framing-error "Frame exceeds ~D bytes" +max-frame-length+)))
            ((and (= byte 32) (plusp digits)) (return length))
            (t (framing-error "Expected decimal length followed by a space")))
      (setf byte (read-byte stream nil nil)))))

(defun read-field (stream &key strict)
  "Read a length-prefixed field from Genera's response (binary stream).
Reject malformed, truncated, and oversized frames. Retain diagnostic byte
replacement and newline normalization."
  (let* ((length (read-frame-length stream))
         (data (read-exactly stream
                             (make-array length :element-type '(unsigned-byte 8)))))
      (when strict
        (return-from read-field
          (mcp-v1-check-text (map 'string #'code-char data))))
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
                        (incf i)))))))

(defun check-wire-string (string)
  "Accept bounded ASCII text: tab, LF, CR, and printable characters."
  (when (> (length string) +max-frame-length+)
    (framing-error "Frame exceeds ~D bytes" +max-frame-length+))
  (loop for ch across string
        for code = (char-code ch)
        for index from 0
        unless (or (<= 32 code 126) (member code '(9 10 13)))
          do (framing-error "Unsupported input character at offset ~D (code ~D)"
                            index code))
  string)

(defun string-to-octets (string)
  "Convert bounded, supported ASCII text to a byte vector."
  (check-wire-string string)
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
  (let* ((clean-expr (strip-lisp-comments (check-wire-string expression)))
         (expr-bytes (string-to-octets clean-expr))
         (header (string-to-octets (format nil "~D " (length expr-bytes))))
         (socket (usocket:socket-connect host port :element-type '(unsigned-byte 8))))
    (unwind-protect
        (let ((stream (usocket:socket-stream socket)))
          ;; Send: length + space + expression
          (write-sequence header stream)
          (write-sequence expr-bytes stream)
          (force-output stream)
          ;; Read three response fields
          (values (read-field stream)
                  (read-field stream)
                  (read-field stream)))
      (usocket:socket-close socket))))

(defun genera-write-envelope (stream request)
  "Write one complete version-1 request on the caller's owned stream."
  (let* ((payload (string-to-octets (mcp-v1-encode request :request)))
         (header (string-to-octets (format nil "~D " (length payload)))))
    (write-sequence header stream)
    (write-sequence payload stream)
    (force-output stream)))

(defun genera-exchange-envelope (stream request)
  "Exchange data on an already selected version-1 connection."
  (genera-write-envelope stream request)
  (let ((response (mcp-v1-decode (read-field stream :strict t) :response)))
    (if (eq (getf request :op) :hello)
        (mcp-v1-check-hello-response request response)
        response)))

(defun genera-send-envelope (request &key (host *genera-host*) (port *genera-port*))
  "Exchange one explicitly selected version-1 envelope; never fall back to eval."
  (mcp-v1-encode request :request)
  (let ((socket (usocket:socket-connect host port :element-type '(unsigned-byte 8))))
    (unwind-protect
        (genera-exchange-envelope (usocket:socket-stream socket) request)
      (usocket:socket-close socket))))

(defvar *genera-request-nonce*
  (format nil "~D-~D" (get-universal-time) (random (expt 2 64) (make-random-state t))))

(load (merge-pathnames "genera-session.lisp" *load-truename*))

(defun genera-eval-envelope (form &key (host *genera-host* host-supplied-p)
                                     (port *genera-port* port-supplied-p)
                                     (token nil token-supplied-p)
                                     (expected-world nil world-supplied-p) package
                                     (print-level 6) (print-length 100) session)
  "Evaluate on SESSION, or own a temporary session. Never retry execution."
  (when (and session (or host-supplied-p port-supplied-p token-supplied-p world-supplied-p))
    (error "Borrowed sessions supply their own connection and authentication settings"))
  (let* ((hello (append (list :version 1 :id "validate-hello" :op :hello :client "lisp-mcp")
                        (when token (list :token token))
                        (when expected-world (list :expected-world expected-world))))
         (request (append (list :version 1 :id "validate-eval" :op :eval
                                :boot-id "pending" :service-id "pending" :form form
                                :print-level print-level :print-length print-length)
                          (when package (list :package package)))))
    ;; Validate all caller data before connecting or sending hello.
    (mcp-v1-encode hello :request)
    (mcp-v1-encode request :request)
    (let ((owned (null session)))
      (handler-case
          (unwind-protect
              (progn
                (unless session
                  (setf session (genera-open-session :host host :port port :token token
                                                     :expected-world expected-world)))
                (apply #'genera-call session :eval
                       (append (list :form form :print-level print-level :print-length print-length)
                               (when package (list :package package)))))
            (when (and owned session) (genera-close-session session)))
        (genera-session-error (condition)
          (or (genera-session-error-response condition) (error condition)))))))

;;; --- Swank client ---
;;; Protocol: length-prefixed S-expressions over TCP.
;;; Each message is a 6-char hex length followed by the S-expression payload.
;;; We send :emacs-rex with swank:eval-and-grab-output, then read messages
;;; until we get a :return reply.

(defun swank-read-message (stream)
  "Read one Swank wire message: 6-char hex length then payload. Returns the parsed sexp.
If the payload cannot be parsed (e.g. unknown package symbols in backtraces),
falls back to extracting just the message keyword from the raw string."
  (let ((header (make-string 6)))
    (read-exactly stream header)
    (unless (every (lambda (ch) (digit-char-p ch 16)) header)
      (framing-error "Expected six hexadecimal Swank length digits"))
    (let* ((len (parse-integer header :radix 16))
           (payload (progn
                      (when (> len +max-frame-length+)
                        (framing-error "Swank frame exceeds ~D characters"
                                       +max-frame-length+))
                      (make-string len))))
      (read-exactly stream payload)
      (handler-case (read-from-string payload)
        (error ()
          ;; Extract the message keyword so the dispatch loop can at least
          ;; identify the message type (e.g. :debug, :write-string, etc.)
          (let ((start (position #\: payload)))
            (if start
                (let ((end (position-if
                            (lambda (c) (member c '(#\Space #\) #\Newline)))
                            payload :start (1+ start))))
                  (list (intern (string-upcase (subseq payload (1+ start) end))
                                :keyword)))
                (list :unknown-message))))))))

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
                (error-message nil)
                (abort-restart-index nil))
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
                                                 (format nil "Aborted: ~A" (second value))) t)))
                         (otherwise
                          (return (values "" (format nil "Unexpected return: ~S" value) t)))))))
                  (:debug
                   ;; (:debug thread level (desc ...) restarts ...)
                   ;; Extract error description and find the *ABORT restart index.
                   (let ((desc (fourth msg))
                         (restarts (fifth msg)))
                     (when (and desc (listp desc))
                       (setf error-message (first desc)))
                     (setf abort-restart-index
                           (or (position-if
                                (lambda (r)
                                  (and (listp r) (stringp (first r))
                                       (member (first r) '("*ABORT" "ABORT")
                                               :test #'string=)))
                                restarts)
                               ;; Fallback if we couldn't parse restarts
                               nil))))
                  (:debug-activate
                   ;; Abort the debugger using the *ABORT restart.
                   (let* ((thread (second msg))
                          (level (third msg))
                          (abort-id (incf *swank-counter*))
                          (restart-idx (or abort-restart-index 1)))
                     (swank-send-raw stream
                       "(:emacs-rex (swank:invoke-nth-restart-for-emacs ~D ~D) ~S ~D ~D)"
                       level restart-idx package thread abort-id)))
                  (:write-string
                   (push (second msg) output-parts))
                  (:read-string
                   ;; Interactive prompt (e.g. y-or-n-p during compilation).
                   ;; Send a newline to dismiss it.
                   (let ((thread (second msg))
                         (tag (third msg)))
                     (swank-send-raw stream
                       "(:emacs-return-string ~D ~D \"~A\")"
                       thread tag (string #\Newline))))
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
  "Read one JSON-RPC message from stdin. Returns :EOF at end of input."
  (loop for line = (read-line *standard-input* nil nil)
        do (unless line (return :eof))
        unless (every (lambda (ch) (find ch '(#\Space #\Tab #\Return))) line)
          do (return
               (with-input-from-string (input line)
                 (let ((value (yason:parse input :json-arrays-as-vectors t
                                                :json-booleans-as-symbols t)))
                   (unless (loop for ch = (read-char input nil nil) while ch
                                 always (find ch '(#\Space #\Tab #\Return)))
                     (error "Trailing JSON data"))
                   value)))))

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

(defun generic-tools ()
  (let ((tools (list (make-host-cl-tool))))
    (when *swank-enabled*
      (push (make-swank-tool) tools))
    (when *genera-enabled*
      (push (make-genera-tool) tools))
    tools))

(defun eval-genera (expression)
  "Evaluate EXPRESSION on the remote Genera machine. Returns a formatted string."
  (handler-case
      (sb-ext:with-timeout *genera-timeout*
        (multiple-value-bind (result stdout stderr)
            (genera-send expression)
          (values (format-response result stdout stderr)
                  (and (zerop (length result)) (plusp (length stderr))))))
    (sb-ext:timeout ()
      (values (format nil "Timeout: Genera did not respond within ~Ds" *genera-timeout*) t))
    (framing-error (e)
      (values (format nil "Protocol error: ~A" e) t))
    (error (e)
      (values (format nil "Connection error: ~A" e) t))))

(defun eval-swank-tool (expression)
  "Evaluate EXPRESSION via a remote Swank server. Returns a formatted string."
  (handler-case
      (sb-ext:with-timeout *genera-timeout*
        (multiple-value-bind (result output failed)
            (swank-eval expression)
          (values (format-response result output "") failed)))
    (sb-ext:timeout ()
      (values (format nil "Timeout: Swank did not respond within ~Ds" *genera-timeout*) t))
    (framing-error (e)
      (values (format nil "Protocol error: ~A" e) t))
    (error (e)
      (values (format nil "Connection error: ~A" e) t))))

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
      (values (format nil "Timeout: evaluation did not complete within ~Ds" *genera-timeout*) t))
    (error (e)
      (values (format nil "Error: ~A" e) t))))

(load (merge-pathnames "mcp-runtime.lisp" *load-truename*))

;;; Run via: sbcl --noinform --non-interactive --load lisp-mcp.lisp --eval '(lisp-mcp:main)'
