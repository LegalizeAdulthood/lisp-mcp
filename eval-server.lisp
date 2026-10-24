;;; -*- Mode: LISP; Syntax: Common-Lisp; Package: CL-USER -*-
;;; mcp-server.lisp - TCP eval server for Genera
;;;
;;; Protocol: length-prefixed messages over a TCP character stream.
;;; The stream transmits raw Genera char-codes on the wire.
;;; We translate to/from ASCII manually using char-to-ascii / code-char.
;;; Lengths are bounded ASCII decimal integers followed by one space.
;;;
;;; Request:  <length><space><expression bytes>
;;; Response: <newline><length><space><result bytes>
;;;           <newline><length><space><stdout bytes>
;;;           <newline><length><space><stderr bytes>
;;; With *MCP-PROTOCOL* = :V1, each frame is one restricted data envelope.
;;; Load genera-protocol.lisp in COMMON-LISP-USER before this file.

;;; --- Length-delimited framing ---
;;; Digit char-codes match ASCII; no Lisp reader is used for headers.

(defconstant +mcp-max-header-digits+ 10)
(defconstant +mcp-max-frame-length+ 1048576)

(defun mcp-framing-error (control &rest arguments)
  (apply #'error (concatenate 'string "Framing error: " control) arguments))

(defun mcp-read-wire-code (stream &optional eof-ok)
  (let ((ch (send stream :tyi)))
    (cond (ch (char-code ch))
          (eof-ok nil)
          (t (mcp-framing-error "Truncated header or payload")))))

(defun mcp-read-length (stream)
  "Read a bounded decimal length; return NIL only at a clean frame boundary."
  (let ((byte (mcp-read-wire-code stream t))
        (length 0)
        (digits 0))
    (when (null byte) (return-from mcp-read-length nil))
    (cond ((= byte 10) (setf byte (mcp-read-wire-code stream)))
          ((= byte 13)
           (setf byte (mcp-read-wire-code stream))
           (when (= byte 10) (setf byte (mcp-read-wire-code stream)))))
    (loop
      (cond ((<= 48 byte 57)
             (when (> (incf digits) +mcp-max-header-digits+)
               (mcp-framing-error "Length header exceeds ~D digits"
                                  +mcp-max-header-digits+))
             (setf length (+ (* length 10) (- byte 48)))
             (when (> length +mcp-max-frame-length+)
               (mcp-framing-error "Frame exceeds ~D bytes" +mcp-max-frame-length+)))
            ((and (= byte 32) (plusp digits)) (return length))
            (t (mcp-framing-error "Expected decimal length followed by a space")))
      (setf byte (mcp-read-wire-code stream)))))

(defun mcp-write-length (stream value)
  "Preserve PRINT's native leading line break, decimal digits, and space."
  (unless (and (integerp value) (<= 0 value +mcp-max-frame-length+))
    (mcp-framing-error "Invalid output length"))
  (let ((*print-base* 10) (*print-radix* nil))
    (print value stream)))

;;; --- ASCII translation ---
;;; The TCP stream sends raw char-codes on the wire.
;;; char-to-ascii maps Genera chars to ASCII codes (returns the code
;;; unchanged for unmappable chars, so we must range-check).
;;; To send a specific ASCII byte, we use code-char to get the Genera
;;; char whose char-code equals that byte value.

(defun mcp-genera-to-wire (char)
  "Convert a Genera character to the Genera char that sends the right ASCII byte.
Returns #\\? for characters outside ASCII 9,10,13,32-126."
  (let ((ascii (char-to-ascii char)))
    (if (or (<= 32 ascii 126) (= ascii 10) (= ascii 9) (= ascii 13))
        (code-char ascii)
        #\?)))

;;; --- Reading requests ---
;;; The client sends ASCII bytes.  The stream gives us chars whose
;;; char-codes ARE the ASCII byte values (no translation on input).

(defun mcp-read-request (stream &optional (translate t))
  "Read a length-prefixed ASCII string from STREAM. Returns NIL on EOF."
  (let ((len (mcp-read-length stream)))
    (when len
      (let ((str (make-string len)))
        (dotimes (i len)
          ;; char-code of what :tyi returns = the ASCII byte from the client
          ;; ascii-to-char converts that ASCII code to the proper Genera char
          (let ((code (mcp-read-wire-code stream)))
            (unless (or (<= 32 code 126) (member code '(9 10 13)))
              (mcp-framing-error "Unsupported input character at offset ~D" i))
            (setf (aref str i)
                  (if translate (ascii-to-char code) (code-char code)))))
        str))))

;;; --- Writing responses ---

(defun mcp-write-field (stream string)
  "Write STRING as a length-prefixed field, translated to ASCII on the wire."
  (let* ((len (length string))
         (out (progn
                (when (> len +mcp-max-frame-length+)
                  (mcp-framing-error "Frame exceeds ~D bytes" +mcp-max-frame-length+))
                (make-string len))))
    (dotimes (i len)
      (setf (aref out i) (mcp-genera-to-wire (aref string i))))
    (mcp-write-length stream len)
    (write-string out stream)))

;;; --- Eval ---
;;;
;;; Uses a debugger hook to prevent any condition from falling through
;;; to the cold load stream.  The hook signals an error that our
;;; condition-case catches, so we always return a result to the client.

(defvar *mcp-eval-error* nil
  "Bound during mcp-eval to capture the condition that invoked the debugger.")

(defun mcp-debugger-hook (condition hook)
  (declare (ignore hook))
  (throw 'mcp-eval-bail condition))

(defvar *mcp-eval-package* (find-package "COMMON-LISP-USER")
  "Package in which MCP eval reads expressions.
Reset to a better default after the first successful eval if needed.")

(defun mcp-eval (string)
  "Evaluate STRING as a Lisp expression.
Returns three values: result-string, stdout-string, stderr-string.
Redirects all interactive streams and suppresses fdefine warnings.
Uses condition-case as the outer safety net so that errors which bypass
*debugger-hook* (Genera-specific debugger entries, stack overflows, etc.)
are still caught and returned to the client instead of killing the connection.
An inner ansi-common-lisp:handler-case catches ANSI CL conditions (e.g. from
ASDF-loaded libraries using define-condition) which Genera's condition-case
does not recognize."
  (let ((stdout (make-string-output-stream))
        (stderr (make-string-output-stream))
        (query-out (make-string-output-stream)))
    (flet ((flush-query ()
             (let ((query-str (get-output-stream-string query-out)))
               (when (plusp (length query-str))
                 (format stderr "~A" query-str)))))
      (let ((result
              (condition-case (outer-err)
                  (let ((condition
                          (catch 'mcp-eval-bail
                            (let* ((terminal (make-two-way-stream (make-string-input-stream "") query-out))
                                   (*terminal-io* terminal)
                                   (*standard-output* stdout)
                                   (*error-output* stderr)
                                   (*trace-output* stderr)
                                   (*debug-io* terminal)
                                   (*query-io* terminal)
                                   (*debugger-hook* #'mcp-debugger-hook)
                                   (*package* *mcp-eval-package*)
                                   (sys:inhibit-fdefine-warnings :just-warn))
                              (return-from mcp-eval
                                ;; Inner handler-case for ANSI CL conditions
                                (ansi-common-lisp:handler-case
                                    (let ((res (princ-to-string (eval (read-from-string string)))))
                                      (flush-query)
                                      (values res
                                              (get-output-stream-string stdout)
                                              (get-output-stream-string stderr)))
                                  (ansi-common-lisp:error (e)
                                    (format stderr "~A" e)
                                    (flush-query)
                                    (values ""
                                            (get-output-stream-string stdout)
                                            (get-output-stream-string stderr)))))))))
                    ;; We get here only if the debugger hook threw
                    (format stderr "~A" condition)
                    "")
                ;; Outer safety net: catches ANY condition that escaped the
                ;; debugger hook.  We use (error) for CL errors and also add
                ;; a catch-all for Genera-specific conditions (reader errors,
                ;; package errors, etc.) which may not be CL:ERROR subtypes.
                (sys:abort
                  (format stderr "Aborted: ~A" outer-err)
                  "")
                (error
                  (format stderr "~A" outer-err)
                  ""))))
        (flush-query)
        (values result
                (get-output-stream-string stdout)
                (get-output-stream-string stderr))))))

;;; --- Server ---

;;; --- Portable service state ---

(defvar *mcp-protocol* :legacy
  "Explicit listener mode: :LEGACY eval or :V1 data envelopes.")

(defvar *mcp-service* nil)
(defvar *mcp-boot-id* nil)
(defvar *mcp-generation-counter* 0)
(defvar *mcp-current-client* nil)
(defvar *mcp-worker-busy* nil)
(defvar *mcp-listener-installed* nil)

(defun mcp-write-envelope (stream response)
  "Write one raw-ASCII data envelope, without diagnostic replacement."
  (let ((text (mcp-v1-encode response :response)))
    (mcp-write-length stream (length text))
    (write-string text stream)))

(defun mcp-new-generation-id (kind)
  (format nil "~A-~D-~D-~D-~D" kind (get-universal-time)
          (get-internal-real-time) (incf *mcp-generation-counter*)
          (random 268435456)))

(defun mcp-v1-new-session ()
  (list :authorized nil :boot-id nil :service-id nil))

(defun mcp-v1-generation (service)
  (list :boot-id (getf service :boot-id) :service-id (getf service :service-id)))

(defun mcp-v1-failure (request code message &optional service)
  (append (list :version 1 :id (getf request :id) :ok nil)
          (when service (mcp-v1-generation service))
          (list :error (list :code code :message message))))

(defun mcp-v1-service-response (request service)
  (append (list :version 1 :id (getf request :id) :ok t)
          (mcp-v1-generation service)
          (list :world (getf service :world)
                :package (package-name *mcp-eval-package*)
                :busy (not (null *mcp-worker-busy*)) :features '(:status))))

(defun mcp-v1-dispatch (request &optional (session (mcp-v1-new-session))
                                        (service *mcp-service*))
  "Authenticate hello before exposing state; never evaluate request form text."
  (mcp-v1-validate request :request)
  (unless (and service (getf service :running))
    (return-from mcp-v1-dispatch
      (mcp-v1-failure request :service-unavailable "Service is unavailable")))
  (cond
    ((eq (getf request :op) :hello)
     ;; A failed repeated hello revokes the connection's previous authorization.
     (setf (getf session :authorized) nil)
     (unless (or (null (getf service :token))
                 (equal (getf service :token) (getf request :token)))
       (return-from mcp-v1-dispatch
         (mcp-v1-failure request :authentication-failed "Authentication failed")))
     (when (and (getf request :expected-world)
                (not (string= (getf request :expected-world) (getf service :world))))
       (return-from mcp-v1-dispatch
         (mcp-v1-failure request :world-mismatch "Expected world does not match")))
     (setf (getf session :authorized) t
           (getf session :boot-id) (getf service :boot-id)
           (getf session :service-id) (getf service :service-id))
     (mcp-v1-service-response request service))
    ((not (getf session :authorized))
     (mcp-v1-failure request :handshake-required "Hello is required"))
    ((not (and (equal (getf request :boot-id) (getf service :boot-id))
               (equal (getf request :service-id) (getf service :service-id))
               (equal (getf session :boot-id) (getf service :boot-id))
               (equal (getf session :service-id) (getf service :service-id))))
     (mcp-v1-failure request :stale-generation "Generation identity does not match"
                     service))
    ((eq (getf request :op) :status) (mcp-v1-service-response request service))
    (t (mcp-v1-failure request :unsupported-operation "Operation is not available"
                       service))))

(defun mcp-stop-service ()
  "Disable only this service and close its clients; preserve world state."
  (when *mcp-worker-busy* (error "Cannot stop an active execution worker"))
  (when (and *mcp-service* (getf *mcp-service* :running))
    (let ((service *mcp-service*))
      (setf (getf service :running) nil)
      (unwind-protect (mcp-disable-listener)
        (dolist (stream (getf service :clients))
          ;; A management form may finish its reply, then its old loop exits.
          (unless (eq stream *mcp-current-client*) (mcp-close-client stream)))
        (setf (getf service :clients) nil))))
  nil)

(defun mcp-initialize-boot ()
  "Regenerate identity for a boot hook; calling this does not boot the world."
  (mcp-stop-service)
  (setf *mcp-listener-installed* nil
        *mcp-boot-id* (mcp-new-generation-id "boot")))

(defun mcp-start-service (&key (protocol *mcp-protocol*)
                             (token nil token-supplied-p)
                             (world nil world-supplied-p) replace)
  "Start once, or explicitly replace an idle service. Return only its ID."
  (let* ((previous *mcp-service*)
         (selected-token (if token-supplied-p token (getf previous :token)))
         (selected-world (if world-supplied-p world
                             (or (getf previous :world) (mcp-detect-world)))))
    (unless (member protocol '(:legacy :v1)) (error "Unknown MCP protocol mode"))
    (when selected-token
      (unless (and (stringp selected-token) (<= 1 (length selected-token) 4096)
                   (every (lambda (ch) (<= 32 (char-code ch) 126)) selected-token))
        (error "Token must be nonempty printable ASCII text"))
      (unless (eq protocol :v1) (error "Authentication requires version-1 mode")))
    (when selected-world
      (mcp-v1-check-text selected-world)
      (when (zerop (length selected-world)) (error "World name must be nonempty")))
    (when (and (eq protocol :v1) (null selected-world))
      (error "Configure a world name when it cannot be detected"))
    (when (and previous (getf previous :running) (not replace))
      (unless (and (eq protocol (getf previous :protocol))
                   (equal selected-token (getf previous :token))
                   (equal selected-world (getf previous :world)))
        (error "Service configuration changes require explicit replacement"))
      (return-from mcp-start-service (getf previous :service-id)))
    (when *mcp-worker-busy* (error "Cannot replace an active execution worker"))
    (mcp-stop-service)
    (unless *mcp-boot-id* (setf *mcp-boot-id* (mcp-new-generation-id "boot")))
    (let ((service (list :running t :protocol protocol :token selected-token
                         :world selected-world :boot-id *mcp-boot-id*
                         :service-id (mcp-new-generation-id "service") :clients nil)))
      (mcp-enable-listener)
      (setf *mcp-service* service *mcp-protocol* protocol)
      (getf service :service-id))))

;;; --- Genera service adapters ---

(defun mcp-detect-world ()
  ;; VLM-WORLD-PATHNAME is a native accessor macro, not a FUNCALL target.
  ;; Internal symbol syntax permits reading source without these symbols.
  (when (and (fboundp 'sys::vlm-world-pathname) (fboundp 'sys::copy-emb-string))
    (let* ((path (sys::copy-emb-string (sys::vlm-world-pathname)))
           (separator (position-if (lambda (ch) (find ch "/\\:")) path :from-end t)))
      (subseq path (if separator (1+ separator) 0)))))

(defun mcp-enable-listener ()
  (let ((port (tcp:protocol-name-tcp-port :mcp-server nil)))
    (when (and port (/= port 8888)) (error "MCP protocol uses a different port"))
    ;; An existing function may be the temporary bootstrap loader. Install our
    ;; wrapper once per boot, clearing only our own old enabled-service entries.
    (unless *mcp-listener-installed*
      (sys:disable-services '(:mcp-server))
      (let ((sys:inhibit-fdefine-warnings :just-warn))
        (net:define-server :mcp-server (:medium :byte-stream :stream stream)
          (mcp-handle-client stream)))
      (setf *mcp-listener-installed* t))
    (unless port (tcp:add-tcp-port-for-protocol :mcp-server 8888)))
  (unless (member :mcp-server net:*services-enabled*)
    (sys:enable-services '(:mcp-server))))

(defun mcp-disable-listener ()
  (sys:disable-services '(:mcp-server)))

(defun mcp-close-client (stream)
  (condition-case ()
      (ansi-common-lisp:handler-case (close stream :abort t)
        (ansi-common-lisp:error () nil))
    (error nil)))

(defun mcp-handle-client (stream)
  "Service one explicitly selected protocol for the life of a connection."
  (let* ((service *mcp-service*)
         (protocol (getf service :protocol))
         (session (mcp-v1-new-session))
         (*mcp-current-client* stream))
    (unless (and service (getf service :running))
      (mcp-close-client stream)
      (return-from mcp-handle-client nil))
    (push stream (getf service :clients))
    (unwind-protect
        (condition-case (frame-error)
            (ansi-common-lisp:handler-case
                (progn
                  (unless (member protocol '(:legacy :v1))
                    (error "Unknown MCP protocol mode"))
                  (loop while (getf service :running)
                        for request = (mcp-read-request stream (eq protocol :legacy))
                        while (and request (getf service :running))
                        do (case protocol
                             (:legacy
                              (multiple-value-bind (result stdout stderr)
                                  (mcp-eval request)
                                (mcp-write-field stream result)
                                (mcp-write-field stream stdout)
                                (mcp-write-field stream stderr)))
                             (:v1
                              (mcp-write-envelope
                               stream (mcp-v1-dispatch
                                       (mcp-v1-decode request :request)
                                       session service))))
                           (send stream :force-output)))
              (ansi-common-lisp:error () nil))
          (error (format *error-output* "MCP connection closed: ~A~%" frame-error)))
      (setf (getf service :clients) (delete stream (getf service :clients)))
      (mcp-close-client stream))))

(mcp-start-service)
