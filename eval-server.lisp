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

;;; --- Portable evaluation support ---

;; One shared character budget bounds retained output, warnings, and values.
;; Even with every character escaped, the result fits inside one wire frame.
(defconstant +mcp-output-limit+ 65536)
(defconstant +mcp-warning-limit+ 32)
(defstruct mcp-budget (remaining +mcp-output-limit+) truncated replaced)
(defstruct (mcp-capture (:constructor mcp-new-capture (budget &optional stop-p)))
  budget stop-p (data (make-array 64 :element-type 'character
                                 :adjustable t :fill-pointer 0)))

(defun mcp-capture-character (capture character)
  (let ((budget (mcp-capture-budget capture)))
    (if (plusp (mcp-budget-remaining budget))
        (let ((code (and (<= 0 (char-code character) 255)
                         (char-to-ascii character))))
          (unless (and code (or (<= 32 code 126) (member code '(9 10 13))))
            (setf code 63 (mcp-budget-replaced budget) t))
          (decf (mcp-budget-remaining budget))
          (vector-push-extend (code-char code) (mcp-capture-data capture)))
        (progn
          (setf (mcp-budget-truncated budget) t)
          (when (mcp-capture-stop-p capture) (throw 'mcp-output-limit nil)))))
  character)

(defun mcp-capture-text (capture)
  (coerce (mcp-capture-data capture) 'simple-string))

;;; --- Native evaluation adapters ---

(defflavor mcp-output-stream (capture) (si:output-stream)
  (:initable-instance-variables capture))

(defmethod (:tyo mcp-output-stream) (character)
  (mcp-capture-character capture character))

(defun mcp-capture-stream (capture)
  (make-instance 'mcp-output-stream :capture capture))

(defun mcp-call-contained (function)
  "Return success/value or NIL/condition without entering an interactive debugger."
  (condition-case (outer)
      (let ((condition
              (catch 'mcp-eval-bail
                (let ((*debugger-hook* #'mcp-debugger-hook))
                  (ansi-common-lisp:handler-case
                      (return-from mcp-call-contained (values t (funcall function)))
                    (ansi-common-lisp:error (inner)
                      (return-from mcp-call-contained (values nil inner))))))))
        (values nil condition))
    (sys:abort (values nil outer))
    (error (values nil outer))))

(defun mcp-render (value budget &optional (escape t))
  "Bound printing as it happens; contain a broken user printer separately."
  (let* ((capture (mcp-new-capture budget t))
         (stream (mcp-capture-stream capture))
         (success t))
    (catch 'mcp-output-limit
      (setf success
            (mcp-call-contained
             (lambda () (write value :stream stream :escape escape) t))))
    (if success (mcp-capture-text capture) "#<printing failed>")))

(defun mcp-read-line-comment (stream character)
  (declare (ignore character))
  ;; Keep LF and CR distinct inside strings and character literals. Only the
  ;; line-comment reader needs both native line endings; no source is deleted.
  (loop for ch = (read-char stream nil nil)
        while (and ch (not (member (char-code ch) '(138 141)))))
  (values))

(defun mcp-read-eval-form (text)
  (let ((native (map 'string (lambda (ch) (ascii-to-char (char-code ch))) text))
        (*readtable* (copy-readtable nil))
        (ansi-common-lisp:*read-eval* nil)
        (*read-base* 10)
        (eof (list nil)))
    (set-macro-character #\; #'mcp-read-line-comment)
    (set-syntax-from-char (ascii-to-char 10) #\Space)
    (with-input-from-string (stream native)
      (let ((form (read stream t nil)))
        (unless (eq (read stream nil eof) eof) (error "Expected exactly one form"))
        form))))

(defun mcp-v1-eval (request service)
  "Read, evaluate, and print one authorized form with bounded retained output."
  (when (mcp-v1-present-p request :timeout-ms)
    (return-from mcp-v1-eval
      (mcp-v1-failure request :unsupported-operation
                      "Remote execution deadlines are not available" service)))
  (let* ((budget (make-mcp-budget))
         (stdout (mcp-new-capture budget))
         (stderr (mcp-new-capture budget))
         (out (mcp-capture-stream stdout))
         (err (mcp-capture-stream stderr))
         (terminal (make-two-way-stream (make-string-input-stream "") err))
         (package-name (or (getf request :package)
                           (package-name *mcp-eval-package*)))
         (start (get-internal-real-time))
         (phase :package-not-found)
         (warnings nil)
         (printed nil)
         (*standard-input* (make-string-input-stream ""))
         (*standard-output* out) (*error-output* err) (*trace-output* err)
         (*terminal-io* terminal) (*debug-io* terminal) (*query-io* terminal)
         (*print-level* (getf request :print-level 6))
         (*print-length* (getf request :print-length 100))
         (*print-circle* t) (*print-pretty* nil)
         (*print-base* 10) (*print-radix* nil)
         (sys:inhibit-fdefine-warnings :just-warn))
    (multiple-value-bind (success outcome)
        (mcp-call-contained
         (lambda ()
           (let ((*package* (or (find-package package-name)
                               (error "No package named ~A" package-name))))
             (setf package-name (package-name *package*) phase :reader-error)
             (let ((form (mcp-read-eval-form (getf request :form))))
               (setf phase :evaluation-error)
               (ansi-common-lisp:handler-bind
                   ((ansi-common-lisp:warning
                      (lambda (condition)
                        (if (< (length warnings) +mcp-warning-limit+)
                            (push (mcp-render condition budget nil) warnings)
                            (setf (mcp-budget-truncated budget) t))
                        (ansi-common-lisp:muffle-warning condition))))
                 (setf printed
                       (mapcar (lambda (value) (mcp-render value budget))
                               (multiple-value-list (eval form)))))))))
      (let* ((diagnostics (make-mcp-budget :remaining 4096))
             (result-package (mcp-render package-name diagnostics nil))
             (failure (unless success
                        (list :code phase
                              :condition-type (mcp-render (type-of outcome) diagnostics)
                              :message (mcp-render outcome diagnostics nil)))))
        (append (list :version 1 :id (getf request :id) :ok success)
                (mcp-v1-generation service)
                (list :request-state (if success :completed :failed)
                      :values printed :value-count (length printed)
                      :stdout (mcp-capture-text stdout)
                      :stderr (mcp-capture-text stderr)
                      :warnings (nreverse warnings)
                      :package result-package
                      :elapsed-ms (max 0 (floor (* 1000 (- (get-internal-real-time) start))
                                               internal-time-units-per-second))
                      :truncated (or (mcp-budget-truncated budget)
                                     (mcp-budget-truncated diagnostics))
                      :replaced (or (mcp-budget-replaced budget)
                                    (mcp-budget-replaced diagnostics)))
                (when failure (list :error failure)))))))

;;; --- Server ---

;;; --- Portable service state ---

(defvar *mcp-protocol* :legacy
  "Explicit listener mode: :LEGACY eval or :V1 data envelopes.")
(defconstant +mcp-listener-source-version+ 1)
(defvar *mcp-autostart* t
  "Bind NIL while loading definitions without starting runtime resources.")

(defvar *mcp-service* nil)
(defvar *mcp-boot-id* nil)
(defvar *mcp-generation-counter* 0)
(defvar *mcp-current-client* nil)
(defvar *mcp-worker-busy* nil)
(defvar *mcp-listener-installed* nil)
(defvar *mcp-worker* nil)
(defvar *mcp-worker-failed* nil)
(defvar *mcp-bootstrap-service* nil)
(defvar *mcp-connections* nil)
(defvar *mcp-current-connection* nil)
(defconstant +mcp-client-limit+ 16)
(defconstant +mcp-control-limit+ 16)
(defconstant +mcp-history-limit+ 32)
(defconstant +mcp-history-byte-limit+ 4194304)

(defmacro mcp-with-state-lock (&body body)
  `(mcp-call-with-state-lock (lambda () ,@body)))

(defun mcp-execution-busy-p ()
  (or *mcp-worker-busy* *mcp-bootstrap-service* *mcp-worker-failed*))

(defun mcp-active-request-id ()
  (when (consp *mcp-worker-busy*)
    (getf (getf *mcp-worker-busy* :request) :id)))

(defun mcp-admit-client (stream service protocol)
  (mcp-with-state-lock
    (when (and (getf service :running)
               (< (length *mcp-connections*) +mcp-client-limit+))
      (let ((client (list :stream stream :service service :protocol protocol
                          :queue nil :controls 0 :execution nil :closed nil)))
        (push client *mcp-connections*)
        (push stream (getf service :clients))
        client))))

(defun mcp-queue-response (client response &optional job)
  ;; Caller holds the state lock. Execution owns a separate reserved slot.
  (unless (getf client :closed)
    (unless job (incf (getf client :controls)))
    (setf (getf client :queue)
          (nconc (getf client :queue) (list (list :response response :job job))))))

(defun mcp-request-identity (request)
  ;; Property order is irrelevant; omission and an explicit default differ.
  (loop for key in '(:op :form :package :timeout-ms :print-level :print-length)
        collect (list key (mcp-v1-present-p request key) (getf request key))))

(defun mcp-ensure-ledger (service)
  ;; Extend an older live service in place; readers retain this same plist.
  (mcp-with-state-lock
    (dolist (property (list (list :active nil) (list :history nil)
                            (list :history-bytes 0) (list :history-evictions 0)
                            (list :history-limit +mcp-history-limit+)
                            (list :history-byte-limit +mcp-history-byte-limit+)))
      (unless (mcp-v1-present-p service (first property))
        (nconc service property))))
  service)

(defun mcp-find-request (service id)
  ;; The service owns one generation; retired generations are never searched.
  (find id (cons (getf service :active) (getf service :history))
        :key (lambda (entry) (getf entry :id)) :test #'equal))

(defun mcp-request-response (request service entry &optional lookup)
  (let ((result (getf entry :result)))
    (if (and result (not lookup)) result
        (append (list :version 1 :id (getf request :id) :ok t)
                (mcp-v1-generation service)
                (list :target-id (if lookup (getf request :target-id)
                                    (getf request :id))
                      :request-state (if entry (getf entry :state) :unknown))
                (when result (list :result result))))))

(defun mcp-finish-job (job result)
  "Commit a terminal outcome before delivery, retaining no client in history."
  (let* ((service (getf job :service)) (entry (getf job :entry))
         (bytes (when entry (+ (getf entry :bytes)
                              (length (mcp-v1-encode result :response))))))
    (mcp-with-state-lock
      (when entry
        (setf (getf entry :result) result
              (getf entry :state) (getf result :request-state)
              (getf entry :bytes) bytes
              (getf service :active) nil)
        (push entry (getf service :history))
        (setf (getf service :history-bytes) (+ (getf service :history-bytes 0) bytes))
        (loop while (or (> (length (getf service :history))
                           (getf service :history-limit +mcp-history-limit+))
                        (> (getf service :history-bytes)
                           (getf service :history-byte-limit +mcp-history-byte-limit+)))
              do (let ((oldest (car (last (getf service :history)))))
                   (decf (getf service :history-bytes) (getf oldest :bytes))
                   (setf (getf service :history) (butlast (getf service :history)))
                   (setf (getf service :history-evictions)
                         (1+ (getf service :history-evictions 0))))))
      (setf (getf job :result) result (getf job :state) :done)
      (mcp-queue-response (getf job :client) result job)
      (setf *mcp-worker-busy* nil))))

(defun mcp-submit-eval (request service client &optional legacy)
  "Reserve one world-wide execution slot before making work runnable."
  (mcp-with-state-lock
    (unless legacy
      (let ((entry (mcp-find-request service (getf request :id))))
        (when entry
          (return-from mcp-submit-eval
            (if (equal (mcp-request-identity request) (getf entry :identity))
                (mcp-request-response request service entry)
                (mcp-v1-failure request :request-id-conflict
                                "Request ID has a different payload" service)))))
      (when (mcp-v1-present-p request :timeout-ms)
        (return-from mcp-submit-eval
          (mcp-v1-failure request :unsupported-operation
                          "Remote execution deadlines are not available" service))))
    (let ((active (or (mcp-active-request-id)
                      (getf (getf (getf client :execution) :request) :id)))
          (code (cond ((or *mcp-worker-failed* (not (mcp-worker-live-p)))
                       :worker-unavailable)
                      ((or (mcp-execution-busy-p) (getf client :execution)) :busy)
                      ((or (null client) (getf client :closed)
                           (not (getf service :running))) :service-unavailable))))
      (when code
        (return-from mcp-submit-eval
          (if legacy
              (list "" "" (if (eq code :busy) "MCP worker is busy"
                              "MCP worker is unavailable"))
              (append (mcp-v1-failure request code "Execution was not accepted" service)
                      (when active (list :active-request-id active))))))
      (let ((job (list :request request :service service :client client
                       :legacy legacy :state :pending :result nil)))
        (unless legacy
          (let ((entry (list :id (getf request :id)
                             :identity (mcp-request-identity request)
                             :bytes (length (mcp-v1-encode request :request))
                             :state :running :result nil)))
            ;; Publish identity before the execution slot becomes runnable.
            (setf (getf job :entry) entry (getf service :active) entry)))
        (setf (getf client :execution) job *mcp-worker-busy* job)
        nil))))

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
                :busy (not (null (mcp-execution-busy-p)))
                :features '(:status :request-status :eval)
                :history-limit (getf service :history-limit +mcp-history-limit+)
                :history-byte-limit (getf service :history-byte-limit +mcp-history-byte-limit+)
                :history-count (length (getf service :history))
                :history-bytes (getf service :history-bytes 0)
                :history-evictions (getf service :history-evictions 0))
          (when (mcp-active-request-id)
            (list :active-request-id (mcp-active-request-id)))))

(defun mcp-v1-dispatch (request &optional (session (mcp-v1-new-session))
                                        (service *mcp-service*))
  "Authenticate and check generation before dispatching a user form."
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
    ((eq (getf request :op) :request-status)
     (mcp-request-response request service
                           (mcp-find-request service (getf request :target-id)) t))
    ((eq (getf request :op) :eval)
     (mcp-submit-eval request service *mcp-current-connection*))
    (t (mcp-v1-failure request :unsupported-operation "Operation is not available"
                       service))))

(defun mcp-stop-service ()
  "Disable only this service and close its clients; preserve world state."
  (mcp-call-with-lifecycle-lock
   (lambda ()
     (let ((service nil) (streams nil))
       (mcp-with-state-lock
         (when (and (mcp-execution-busy-p) (not (mcp-legacy-worker-owner-p)))
           (error "Cannot stop an active execution worker"))
         (when (and *mcp-service* (getf *mcp-service* :running))
           (setf service *mcp-service* streams (getf service :clients)
                 (getf service :running) nil (getf service :clients) nil)))
       (when service
         (unwind-protect (mcp-disable-listener)
           (dolist (stream streams)
             ;; The current legacy management job may finish its response.
             (unless (eq stream *mcp-current-client*) (mcp-close-client stream)))))
       nil))))

(defun mcp-initialize-boot ()
  "Regenerate identity for a boot hook; calling this does not boot the world."
  (mcp-call-with-lifecycle-lock
   (lambda ()
     (mcp-stop-service)
     (mcp-with-state-lock
       (setf *mcp-listener-installed* nil
             *mcp-boot-id* (mcp-new-generation-id "boot"))))))

(defun mcp-check-service-configuration (protocol token world)
  (unless (member protocol '(:legacy :v1)) (error "Unknown MCP protocol mode"))
  (when token
    (unless (and (stringp token) (<= 1 (length token) 4096)
                 (every (lambda (ch) (<= 32 (char-code ch) 126)) token))
      (error "Token must be nonempty printable ASCII text"))
    (unless (eq protocol :v1) (error "Authentication requires version-1 mode")))
  (when world
    (mcp-v1-check-text world)
    (when (zerop (length world)) (error "World name must be nonempty")))
  t)

(defun mcp-start-service (&key (protocol *mcp-protocol*)
                             (token nil token-supplied-p)
                             (world nil world-supplied-p) replace)
  "Start or replace the listener, retaining its worker. Return only its ID."
  (mcp-call-with-lifecycle-lock
   (lambda ()
     (let* ((previous *mcp-service*)
            (selected-token (if token-supplied-p token (getf previous :token)))
            (selected-world (if world-supplied-p world
                                (or (getf previous :world) (mcp-detect-world)))))
       (mcp-check-service-configuration protocol selected-token selected-world)
       (when (and (eq protocol :v1) (null selected-world))
         (error "Configure a world name when it cannot be detected"))
       (when (and previous (getf previous :running) (not replace))
         (unless (and (eq protocol (getf previous :protocol))
                      (equal selected-token (getf previous :token))
                      (equal selected-world (getf previous :world)))
           (error "Service configuration changes require explicit replacement"))
         (mcp-ensure-ledger previous)
         (mcp-ensure-worker)
         (mcp-enable-listener)
         (return-from mcp-start-service (getf previous :service-id)))
       (mcp-with-state-lock
         (when (and (mcp-execution-busy-p) (not (mcp-legacy-worker-owner-p)))
           (error "Cannot replace an active execution worker")))
       (mcp-stop-service)
       (unless *mcp-boot-id* (setf *mcp-boot-id* (mcp-new-generation-id "boot")))
       (let ((service (list :running t :protocol protocol :token selected-token
                            :world selected-world :boot-id *mcp-boot-id*
                            :service-id (mcp-new-generation-id "service") :clients nil
                            :asynchronous t)))
         (mcp-ensure-ledger service)
         (mcp-ensure-worker)
         (mcp-enable-listener)
         (mcp-with-state-lock
           (setf *mcp-service* service *mcp-protocol* protocol))
         (getf service :service-id))))))

;;; --- Genera service adapters ---

(defvar *mcp-state-lock* (process:make-lock "MCP state" :recursive t))
(defvar *mcp-lifecycle-lock* (process:make-lock "MCP lifecycle" :recursive t))

(defun mcp-call-with-state-lock (function)
  (process:with-lock (*mcp-state-lock*) (funcall function)))

(defun mcp-call-with-lifecycle-lock (function)
  (process:with-lock (*mcp-lifecycle-lock*) (funcall function)))

(defun mcp-worker-live-p ()
  (and *mcp-worker* (scl:process-active-p *mcp-worker*)))

(defun mcp-legacy-worker-owner-p ()
  ;; A legacy management form may replace its own listener. Its worker and
  ;; admission slot survive until that form returns; other evals remain busy.
  (and (eq scl:*current-process* *mcp-worker*)
       (consp *mcp-worker-busy*) (getf *mcp-worker-busy* :legacy)))

(defun mcp-ensure-worker ()
  (mcp-with-state-lock
    (when (and *mcp-service* (not (getf *mcp-service* :asynchronous)))
      (error "Complete the listener upgrade before starting an execution worker"))
    (cond (*mcp-worker*
           (unless (and (mcp-worker-live-p) (not *mcp-worker-failed*))
             (error "MCP execution worker is unavailable; it was not replaced")))
          (t
           (setf *mcp-worker*
                 (scl:process-run-function
                  '(:name "MCP execution" :warm-boot-action :flush)
                  #'mcp-worker-loop))))))

(defun mcp-worker-failure (job condition)
  (if (getf job :legacy)
      (list "" "" "MCP execution failed")
      (let* ((request (getf job :request))
             (budget (make-mcp-budget :remaining 4096)))
        (append (mcp-v1-failure request :evaluation-error
                                (mcp-render condition budget nil) (getf job :service))
                (list :request-state :failed :values nil :value-count 0
                      :stdout "" :stderr "" :warnings nil :package ""
                      :elapsed-ms 0 :truncated (mcp-budget-truncated budget)
                      :replaced (mcp-budget-replaced budget))))))

(defun mcp-execute-job (job)
  (let* ((client (getf job :client))
         (*mcp-current-client* (getf client :stream))
         (*mcp-current-connection* client))
    (multiple-value-bind (success result)
        (mcp-call-contained
         (lambda ()
           (if (getf job :legacy)
               (multiple-value-list (mcp-eval (getf (getf job :request) :form)))
               (mcp-v1-eval (getf job :request) (getf job :service)))))
      (unless success
        (setf result (mcp-worker-failure job result))
        (unless (getf job :legacy)
          (setf (getf (getf result :error) :condition-type) "MCP-WORKER-ERROR")))
      (mcp-finish-job job result))))

(defun mcp-run-job (job)
  ;; Keep this boundary outside argument setup and completion bookkeeping,
  ;; including for a worker already running an older loop after source reload.
  (let ((success (mcp-call-contained (lambda () (mcp-execute-job job)))))
    (unless success
      (mcp-with-state-lock
        (when (eq scl:*current-process* *mcp-worker*)
          (setf *mcp-worker-failed* t))))
    success))

(defun mcp-worker-loop ()
  (unwind-protect
      (mcp-call-contained
       (lambda ()
         (progn
           ;; A pre-worker source-loading handler must finish before any new eval.
           (when *mcp-bootstrap-service*
             (scl:process-wait "MCP upgrade drain"
                               (lambda () (null (getf *mcp-bootstrap-service* :clients))))
             (mcp-with-state-lock (setf *mcp-bootstrap-service* nil)))
           (loop
             (scl:process-wait "MCP execution"
                               (lambda () (and (consp *mcp-worker-busy*)
                                               (eq (getf *mcp-worker-busy* :state) :pending))))
             (let ((job (mcp-with-state-lock
                          (setf (getf *mcp-worker-busy* :state) :running)
                          *mcp-worker-busy*)))
               (unless (mcp-run-job job) (return)))))))
    ;; Preserve an unresolved slot; never create another worker beside it.
    (mcp-with-state-lock
      ;; A saved worker may unwind after native boot created its successor.
      ;; Its cleanup must not poison that fresh service's execution slot.
      (when (eq scl:*current-process* *mcp-worker*)
        (setf *mcp-worker-failed* t)))))

(defun mcp-close-connection (client)
  (let ((stream nil))
    (mcp-with-state-lock
      (unless (getf client :closed)
        (setf stream (getf client :stream) (getf client :closed) t
              (getf client :queue) nil)
        (setf *mcp-connections* (delete client *mcp-connections*)
              (getf (getf client :service) :clients)
              (delete stream (getf (getf client :service) :clients)))))
    (when stream (mcp-close-stream stream))))

(defun mcp-client-writer (client)
  "The only production response writer for this connection."
  (unwind-protect
      (mcp-call-contained
       (lambda ()
         (loop
           (scl:process-wait "MCP response"
                             (lambda () (or (getf client :closed) (getf client :queue))))
           (when (getf client :closed) (return))
           (let* ((entry (mcp-with-state-lock (pop (getf client :queue))))
                  (response (getf entry :response)) (job (getf entry :job))
                  (stream (getf client :stream)))
             (if (eq (getf client :protocol) :legacy)
                 (dolist (field response) (mcp-write-field stream field))
                 (mcp-write-envelope stream response))
             (send stream :force-output)
             (mcp-with-state-lock
               (if job (setf (getf client :execution) nil)
                   (decf (getf client :controls))))
             (when (and (not (getf (getf client :service) :running))
                        (null (getf client :execution)))
               (return))))))
    (mcp-close-connection client)))

(defun mcp-upgrade-service ()
  "Drain a pre-worker loading connection without changing generation identity."
  (when (and *mcp-service* (getf *mcp-service* :running)
             (not (getf *mcp-service* :asynchronous)))
    (mcp-with-state-lock
      (let ((old *mcp-service*))
        (unless (every (lambda (stream) (eq stream *mcp-current-client*))
                       (getf old :clients))
          (error "Close other MCP clients before upgrading the listener"))
        (setf (getf old :running) nil
              *mcp-bootstrap-service* old
              *mcp-service* (copy-list old)
              (getf *mcp-service* :running) t
              (getf *mcp-service* :clients) nil
              (getf *mcp-service* :asynchronous) t)))))

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

(defun mcp-close-stream (stream)
  (condition-case ()
      (ansi-common-lisp:handler-case (close stream :abort t)
        (ansi-common-lisp:error () nil))
    (error nil)))

(defun mcp-close-client (stream)
  (let ((client (mcp-with-state-lock
                  (find stream *mcp-connections* :key (lambda (c) (getf c :stream))))))
    (if client (mcp-close-connection client) (mcp-close-stream stream))))

(defun mcp-handle-client (stream)
  "Read and dispatch independently of execution and the response writer."
  (let* ((service *mcp-service*)
         (protocol (getf service :protocol))
         (session (mcp-v1-new-session))
         (client (mcp-admit-client stream service protocol))
         (*mcp-current-connection* client)
         (*mcp-current-client* stream))
    (unless client
      (mcp-close-client stream)
      (return-from mcp-handle-client nil))
    (unwind-protect
        (progn
          (scl:process-run-function
           '(:name "MCP response" :warm-boot-action :flush)
           #'mcp-client-writer client)
          (mcp-call-contained
           (lambda ()
             (loop while (and (getf service :running) (not (getf client :closed)))
                   do (scl:process-wait "MCP control space"
                        (lambda () (or (getf client :closed)
                                       (< (getf client :controls) +mcp-control-limit+))))
                      (when (getf client :closed) (return))
                      (let ((text (mcp-read-request stream (eq protocol :legacy))))
                        (unless text (return))
                        (mcp-with-state-lock
                          (when (and (getf service :running) (not (getf client :closed)))
                            (let ((response
                                    (case protocol
                                      (:v1 (mcp-v1-dispatch (mcp-v1-decode text :request)
                                                            session service))
                                      (:legacy
                                       (mcp-submit-eval
                                        (list :id (mcp-new-generation-id "legacy") :form text)
                                        service client t))
                                      (otherwise (error "Unknown MCP protocol mode")))))
                              (when response (mcp-queue-response client response))))))
                      ;; Legacy replies have no IDs: preserve their request
                      ;; order instead of passing a later busy reply first.
                      (when (and (eq protocol :legacy) (getf client :execution))
                        (scl:process-wait "MCP legacy response"
                          (lambda () (or (getf client :closed)
                                         (null (getf client :execution)))))))
             ;; The worker's legacy management job may have stopped its own
             ;; service; let its reserved response drain before closing it.
             (when (and (not (getf service :running)) (getf client :execution))
               (scl:process-wait "MCP final response"
                 (lambda () (or (getf client :closed)
                                (null (getf client :execution)))))))))
      (mcp-close-connection client))))

;; Compile process entry points and their lock continuations before starting
;; them. Native process/lock machinery must not depend on interpreter frames.
(mapc #'compile
      '(mcp-call-with-state-lock mcp-call-with-lifecycle-lock
        mcp-admit-client mcp-ensure-ledger mcp-submit-eval mcp-finish-job mcp-v1-dispatch
        mcp-stop-service mcp-initialize-boot mcp-start-service
        mcp-ensure-worker mcp-execute-job mcp-run-job mcp-worker-loop
        mcp-close-connection mcp-client-writer mcp-handle-client))

(when *mcp-autostart*
  (mcp-upgrade-service)
  (mcp-start-service))
