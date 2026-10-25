;;; Portable listener definitions with explicit Genera API substitutes.

(defpackage #:genera-protocol-fixture (:use #:cl))
(in-package #:genera-protocol-fixture)
(load (merge-pathnames "../genera-protocol.lisp" *load-truename*))

(defvar *mcp-eval-package* (find-package "COMMON-LISP-USER"))
(defvar *mcp-default-package* (find-package "COMMON-LISP-USER"))
(defvar *fixture-enables* 0)
(defvar *fixture-disables* 0)
(defvar *fixture-closed* nil)
(defvar *fixture-world* "fixture.vlod")
(defvar *fixture-worker-live* t)
(defvar *fixture-worker-owner* nil)
(defvar *mcp-interruptible-job* nil)

(defun send (stream operation)
  (ecase operation (:tyi (read-char stream nil nil))))
(defun ascii-to-char (code)
  (code-char (case code (10 138) (13 141) (9 137) (otherwise code))))
(defun char-to-ascii (character)
  (case (char-code character)
    (138 10) (141 13) (137 9) (otherwise (char-code character))))
(defun mcp-detect-world () *fixture-world*)
(defun mcp-enable-listener () (incf *fixture-enables*))
(defun mcp-disable-listener () (incf *fixture-disables*))
(defun mcp-close-client (stream) (push stream *fixture-closed*))
(defun mcp-call-with-state-lock (function) (funcall function))
(defun mcp-call-with-lifecycle-lock (function) (funcall function))
(defun mcp-worker-live-p () *fixture-worker-live*)
(defun mcp-legacy-worker-owner-p () *fixture-worker-owner*)
(defun mcp-ensure-worker () nil)
(defun mcp-interrupt-supported-p () nil)
(defun mcp-schedule-interrupt (job) (declare (ignore job))
  (error "Native interruption requires a live Genera acceptance check"))
(defun mcp-v1-eval (request service)
  (declare (ignore request service))
  (error "Native evaluation requires the live Genera acceptance check"))

;; Read framing definitions without reading any Genera-only eval forms.
(with-open-file (source (merge-pathnames "../eval-server.lisp" *load-truename*))
  (loop for form = (read source nil nil)
        while (and form (not (eq (first form) 'defvar)))
        do (eval form)))

;; Extract the actual portable collector and service definitions.
(dolist (markers '((";;; --- Portable evaluation support ---"
                    ";;; --- Native evaluation adapters ---")
                   (";;; --- Portable service state ---"
                    ";;; --- Genera service adapters ---")))
 (let ((text
        (with-output-to-string (out)
          (with-open-file (source (merge-pathnames "../eval-server.lisp" *load-truename*))
            (loop with copying = nil
                  for line = (read-line source nil nil)
                  while line
                  for clean = (string-right-trim '(#\Return) line)
                  until (string= clean (second markers))
                  do (when (string= clean (first markers))
                       (setf copying t))
                     (when copying (write-line line out)))))))
  (with-input-from-string (source text)
    (loop for form = (read source nil nil) while form do (eval form)))))
